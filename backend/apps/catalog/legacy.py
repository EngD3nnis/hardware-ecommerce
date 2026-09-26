"""Import the original static website catalogue (dewmix_source/) into the database.

Source of truth for the import:
- products: the `const CATALOG=[...]` JSON array inside index.html
  (products.html holds an identical copy, verified in the Stage 1 audit);
- images: imgs/<legacy id>.jpg. images.js holds different encodes of the
  same photos and is not used.

Guarantees:
- Idempotent: products are matched by legacy_id and images by SHA-256, so
  re-running creates nothing new.
- Non-destructive: an existing product is never overwritten. If the source
  and the database disagree, the difference is reported and left for a person.
- Suspicious data is not "fixed" automatically. It becomes CatalogReviewItems
  and CatalogChangeProposals.
"""

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from django.db import transaction

from apps.core.actors import Actor
from apps.core.exceptions import Conflict, ValidationError

from . import media as media_utils
from . import services
from .models import Category, Product, ProductMedia, ProductStatus, ReviewKind, normalise_sku

SOURCE = "legacy_import"
CATALOG_MARKER = "const CATALOG="
# The static site cut long names at 48-50 characters (lengths spike there and
# the samples end mid-word); names of 47 characters and shorter look complete.
TRUNCATION_LENGTHS = range(48, 51)
# SEO copy that leaked into product names, e.g. "... Tumbler Holder in Nairobi Keny".
SEO_NAME_PATTERN = re.compile(r"\b(in nairobi|nairobi|kenya|keny)\b", re.IGNORECASE)
GENERIC_SUBCATEGORIES = {"general"}
# Brand names that start product names in the legacy data and are not also
# ordinary words (e.g. "Union" is a pipe fitting as well as a lock brand).
KNOWN_BRAND_PREFIXES = ("Pattex", "Yale", "Crown", "Wadfow")


@dataclass(frozen=True)
class LegacyItem:
    id: int
    sku: str
    category: str
    subcategory: str
    name: str


@dataclass
class LegacyImportReport:
    dry_run: bool = False
    rows: int = 0
    categories_created: int = 0
    products_created: int = 0
    products_unchanged: int = 0
    products_conflicting: list = field(default_factory=list)  # legacy ids whose DB data differs
    rejected: list = field(default_factory=list)  # (legacy id, reason)
    images_found: int = 0
    images_stored: int = 0
    images_attached: int = 0
    images_missing: list = field(default_factory=list)  # legacy ids without an image file
    images_rejected: list = field(default_factory=list)  # (file, reason)
    images_without_product: list = field(default_factory=list)
    review_items_created: int = 0
    proposals_created: int = 0

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def read_legacy_catalog(source_dir: Path) -> list[LegacyItem]:
    html = (source_dir / "index.html").read_text(encoding="utf-8")
    start = html.find(CATALOG_MARKER)
    if start == -1:
        raise ValueError(f"{CATALOG_MARKER!r} not found in {source_dir / 'index.html'}")
    data, _ = json.JSONDecoder().raw_decode(html, start + len(CATALOG_MARKER))
    return [
        LegacyItem(
            id=int(row["id"]),
            sku=str(row["sku"]),
            category=str(row["cat"]),
            subcategory=str(row.get("sub") or ""),
            name=str(row["name"]),
        )
        for row in data
    ]


def import_legacy_catalog(
    source_dir: Path, actor: Actor | None = None, *, dry_run: bool = False, with_images: bool = True
) -> LegacyImportReport:
    """Import products (and optionally images). Dry run = do everything, then roll back."""
    actor = actor or Actor.system("legacy_import")
    report = LegacyImportReport(dry_run=dry_run)
    items = read_legacy_catalog(source_dir)
    report.rows = len(items)
    with transaction.atomic():
        products = _import_products(items, actor, report)
        _flag_catalogue_issues(items, products, actor, report)
        if with_images:
            _import_images(source_dir / "imgs", products, actor, report, dry_run=dry_run)
        if dry_run:
            transaction.set_rollback(True)
    return report


def _category_for(item: LegacyItem, actor: Actor, report: LegacyImportReport) -> Category:
    top, created = services.get_or_create_category(item.category, actor)
    report.categories_created += created
    sub_name = services.clean_name(item.subcategory)
    if not sub_name or sub_name.lower() == top.name.lower():
        return top
    sub, created = services.get_or_create_category(sub_name, actor, parent=top)
    report.categories_created += created
    return sub


def _import_products(items: list[LegacyItem], actor: Actor, report: LegacyImportReport) -> dict[int, Product]:
    existing = {p.legacy_id: p for p in Product.objects.filter(legacy_id__in=[i.id for i in items])}
    products: dict[int, Product] = {}
    for item in items:
        category = _category_for(item, actor, report)
        product = existing.get(item.id)
        if product is not None:
            products[item.id] = product
            differs = (
                product.sku != normalise_sku(item.sku)
                or product.name != services.clean_name(item.name)
                or product.category_id != category.pk
            )
            if differs:
                report.products_conflicting.append(item.id)
            else:
                report.products_unchanged += 1
            continue
        try:
            with transaction.atomic():
                product = services.create_product(
                    actor,
                    sku=item.sku,
                    name=item.name,
                    category=category,
                    legacy_id=item.id,
                    status=ProductStatus.ACTIVE,  # already published on the website
                    price_on_request=True,  # the website never showed prices
                )
        except (ValidationError, Conflict) as exc:
            report.rejected.append((item.id, exc.message))
            continue
        products[item.id] = product
        report.products_created += 1
    return products


def _flag(report: LegacyImportReport, **kwargs) -> None:
    _, created = services.flag_for_review(source=SOURCE, **kwargs)
    report.review_items_created += created


def _flag_catalogue_issues(
    items: list[LegacyItem], products: dict[int, Product], actor: Actor, report: LegacyImportReport
) -> None:
    by_name = defaultdict(list)
    for item in items:
        if item.id in products:
            by_name[services.clean_name(item.name).lower()].append(products[item.id])

    for name, group in by_name.items():
        if len(group) > 1:
            skus = sorted(p.sku for p in group)
            _flag(
                report,
                kind=ReviewKind.DUPLICATE_NAME,
                fingerprint=f"legacy:duplicate-name:{name}",
                summary=f"{len(group)} products named “{group[0].name}” ({', '.join(skus)})",
                products=group,
                details={"skus": skus, "hint": "Merge them, or make the names say what differs (size, finish…)."},
            )

    generic = defaultdict(list)
    for item in items:
        product = products.get(item.id)
        if product is None:
            continue
        if len(item.name) in TRUNCATION_LENGTHS:
            _flag(
                report,
                kind=ReviewKind.TRUNCATED_NAME,
                fingerprint=f"legacy:truncated-name:{item.id}",
                summary=f"{product.sku}: name may be cut off: “{product.name}”",
                products=[product],
                details={"length": len(item.name)},
            )
        if SEO_NAME_PATTERN.search(item.name):
            _flag(
                report,
                kind=ReviewKind.SUSPICIOUS_VALUE,
                fingerprint=f"legacy:seo-name:{item.id}",
                summary=f"{product.sku}: name contains location/SEO words: “{product.name}”",
                products=[product],
                details={"hint": "Remove words like “in Nairobi Kenya” from the product name."},
            )
        if services.clean_name(item.subcategory).lower() in GENERIC_SUBCATEGORIES:
            generic[product.category].append(product)
        for brand in KNOWN_BRAND_PREFIXES:
            if re.match(rf"{brand}\b", product.name, flags=re.IGNORECASE) and product.brand_id is None:
                _, created = services.propose_change(
                    product,
                    "brand",
                    brand,
                    actor,
                    source=SOURCE,
                    reason=f"Product name starts with the brand name “{brand}”.",
                )
                report.proposals_created += created

    for category, group in generic.items():
        _flag(
            report,
            kind=ReviewKind.GENERIC_CATEGORY,
            fingerprint=f"legacy:generic-category:{category.pk}",
            summary=f"{len(group)} products in catch-all subcategory “{category}”",
            products=group,
            details={"hint": "Split into real subcategories (e.g. Taps: basin / sink / wall / bib)."},
        )


def _import_images(
    images_dir: Path, products: dict[int, Product], actor: Actor, report: LegacyImportReport, *, dry_run: bool
) -> None:
    """Store and attach images. In a dry run images are only validated and hashed,
    because files written to storage would survive the database rollback."""
    if not images_dir.is_dir():
        report.images_missing = sorted(products)
        return
    products_by_asset = defaultdict(list)
    files = {}
    for path in images_dir.iterdir():
        match = re.fullmatch(r"(\d+)\.(jpe?g|png|webp)", path.name, flags=re.IGNORECASE)
        if match:
            files[int(match.group(1))] = path
    report.images_found = len(files)

    for legacy_id, path in sorted(files.items()):
        product = products.get(legacy_id)
        if product is None:
            report.images_without_product.append(path.name)
            continue
        data = path.read_bytes()
        try:
            if dry_run:
                media_utils.inspect_image(data)
                sha = media_utils.sha256_hex(data)
                report.images_stored += sha not in products_by_asset
                report.images_attached += 1
                products_by_asset[sha].append(product)
                continue
            asset, stored = services.store_image(data, path.name, trusted=True)
        except ValidationError as exc:
            report.images_rejected.append((path.name, exc.message))
            continue
        report.images_stored += stored
        _, attached = services.attach_image(product, asset, actor, alt_text=product.name)
        report.images_attached += attached
        products_by_asset[asset.sha256].append(product)

    if dry_run:
        with_image = {p.pk for group in products_by_asset.values() for p in group}
    else:
        with_image = set(
            ProductMedia.objects.filter(product__in=products.values()).values_list("product_id", flat=True)
        )
    for legacy_id, product in sorted(products.items()):
        if product.pk not in with_image:
            report.images_missing.append(legacy_id)
            _flag(
                report,
                kind=ReviewKind.MISSING_IMAGE,
                fingerprint=f"legacy:missing-image:{legacy_id}",
                summary=f"{product.sku} {product.name} has no image",
                products=[product],
            )

    for sha, group in products_by_asset.items():
        if len(group) > 1:
            skus = sorted(p.sku for p in group)
            _flag(
                report,
                kind=ReviewKind.DUPLICATE_IMAGE,
                fingerprint=f"legacy:duplicate-image:{sha}",
                summary=f"Same photo used for {len(group)} products: {', '.join(skus)}",
                products=group,
                details={
                    "sha256": sha,
                    "hint": "Fine if they are variants that look identical; otherwise replace the wrong photos.",
                },
            )
