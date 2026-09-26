"""Keep the static website (dewmix_source/) in step with the database.

The static site renders from a `CATALOG` JSON literal embedded in index.html
and products.html. `export_catalog()` regenerates that literal from active
products that have a legacy (website) id, in the same {id, sku, cat, sub,
name} shape, so the site keeps working unchanged while the database becomes
the source of truth. Products without a website id are reported: they have
no photo in imgs/ yet and need one before they can appear on the old site.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from .models import Product, ProductStatus

TARGETS = {"index.html": "const CATALOG=", "products.html": "var CATALOG="}


@dataclass
class ExportReport:
    products: int = 0
    files_changed: list = field(default_factory=list)
    without_website_id: list = field(default_factory=list)


def catalog_rows() -> list[dict]:
    products = (
        Product.objects.filter(status=ProductStatus.ACTIVE, legacy_id__isnull=False)
        .select_related("category", "category__parent")
        .order_by("legacy_id")
    )
    rows = []
    for p in products:
        top = p.category.parent or p.category
        rows.append({"id": p.legacy_id, "sku": p.sku, "cat": top.name, "sub": p.category.name, "name": p.name})
    return rows


def _replace_literal(html: str, marker: str, rows: list[dict]) -> str:
    start = html.find(marker)
    if start == -1:
        raise ValueError(f"{marker!r} not found")
    value_start = start + len(marker)
    _, value_end = json.JSONDecoder().raw_decode(html, value_start)
    literal = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return html[:value_start] + literal + html[value_end:]


def export_catalog(site_dir: Path, *, write: bool) -> ExportReport:
    rows = catalog_rows()
    report = ExportReport(products=len(rows))
    report.without_website_id = list(
        Product.objects.filter(status=ProductStatus.ACTIVE, legacy_id__isnull=True).values_list("sku", flat=True)
    )
    for filename, marker in TARGETS.items():
        path = site_dir / filename
        html = path.read_text(encoding="utf-8")
        updated = _replace_literal(html, marker, rows)
        if updated != html:
            report.files_changed.append(filename)
            if write:
                path.write_text(updated, encoding="utf-8")
    return report
