import json

import pytest

from apps.catalog.legacy import import_legacy_catalog, read_legacy_catalog
from apps.catalog.models import (
    CatalogChangeProposal,
    CatalogReviewItem,
    Category,
    MediaAsset,
    Product,
    ProductMedia,
    ProductStatus,
    ReviewKind,
)
from conftest import make_image_bytes

pytestmark = pytest.mark.django_db

CATALOG = [
    {
        "id": 0,
        "sku": "DWX-0000",
        "cat": "Bathroom utilities & Accessories",
        "sub": "Bathroom Sets",
        "name": "Aluminium Bathroom Set",
    },
    {"id": 1, "sku": "DWX-0001", "cat": "Taps & Faucets", "sub": "General", "name": "Basin Pillar Tap R 16"},
    {"id": 2, "sku": "DWX-0002", "cat": "Taps & Faucets", "sub": "General", "name": "Basin  Pillar Tap R 16"},
    {"id": 5, "sku": "DWX-0005", "cat": "Water Pumps", "sub": "Water Pumps", "name": "Pedrollo Pump"},
    {"id": 7, "sku": "DWX-0007", "cat": "Locks", "sub": "Padlocks", "name": "Yale Padlock 50mm"},
    {
        "id": 9,
        "sku": "DWX-0009",
        "cat": "Bathroom utilities & Accessories",
        "sub": "Bathroom Sets",
        "name": "Full Bathroom Accessories Piece Set in Matt Blac",
    },
    {
        "id": 11,
        "sku": "DWX-0011",
        "cat": "Bathroom utilities & Accessories",
        "sub": "Tumbler",
        "name": "Black Tumbler Holder in Nairobi",
    },
]


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "dewmix_source"
    (root / "imgs").mkdir(parents=True)
    html = "<html><script>const X=1;\nconst CATALOG=" + json.dumps(CATALOG) + ";\nfunction go(){}</script></html>"
    (root / "index.html").write_text(html, encoding="utf-8")
    shared = make_image_bytes(color=(5, 5, 5))
    (root / "imgs" / "0.jpg").write_bytes(make_image_bytes(color=(1, 1, 1)))
    (root / "imgs" / "1.jpg").write_bytes(shared)
    (root / "imgs" / "2.jpg").write_bytes(shared)  # same photo as id 1
    (root / "imgs" / "7.jpg").write_bytes(make_image_bytes(color=(7, 7, 7)))
    (root / "imgs" / "9.jpg").write_bytes(make_image_bytes(color=(9, 9, 9)))
    (root / "imgs" / "11.jpg").write_bytes(make_image_bytes(color=(11, 11, 11)))
    (root / "imgs" / "999.jpg").write_bytes(make_image_bytes(color=(99, 9, 9)))  # no such product
    (root / "imgs" / "notes.txt").write_text("ignored")
    return root


def test_reads_catalog_from_index_html(source):
    items = read_legacy_catalog(source)
    assert [i.id for i in items] == [0, 1, 2, 5, 7, 9, 11]
    assert items[0].sku == "DWX-0000"


def test_imports_products_categories_and_images(source):
    report = import_legacy_catalog(source)
    assert report.products_created == 7
    assert report.rejected == []

    product = Product.objects.get(legacy_id=0)  # id 0 must survive (it was dropped by the old site)
    assert product.sku == "DWX-0000"
    assert product.status == ProductStatus.ACTIVE
    assert product.price_on_request is True
    assert str(product.category) == "Bathroom utilities & Accessories / Bathroom Sets"
    # Subcategory equal to its category → product sits on the top-level category.
    assert str(Product.objects.get(legacy_id=5).category) == "Water Pumps"
    assert Category.objects.filter(parent__isnull=True).count() == 4

    assert report.images_found == 7
    assert report.images_stored == 5  # ids 1 and 2 share one file
    assert MediaAsset.objects.count() == 5
    assert ProductMedia.objects.filter(is_primary=True).count() == 6
    assert report.images_without_product == ["999.jpg"]
    assert report.images_missing == [5]


def test_flags_quality_issues_without_changing_data(source):
    import_legacy_catalog(source)
    kinds = sorted(CatalogReviewItem.objects.values_list("kind", flat=True))
    assert kinds == sorted(
        [
            ReviewKind.DUPLICATE_IMAGE,  # ids 1 & 2
            ReviewKind.DUPLICATE_NAME,  # ids 1 & 2 (after whitespace normalisation)
            ReviewKind.GENERIC_CATEGORY,  # Taps / General
            ReviewKind.MISSING_IMAGE,  # id 5
            ReviewKind.TRUNCATED_NAME,  # id 9 (48 chars)
            ReviewKind.SUSPICIOUS_VALUE,  # id 11 "in Nairobi"
        ]
    )
    proposal = CatalogChangeProposal.objects.get()
    assert (proposal.product.legacy_id, proposal.field, proposal.proposed_value) == (7, "brand", "Yale")
    assert Product.objects.get(legacy_id=7).brand is None  # proposed, not applied


def test_rerun_is_idempotent(source):
    import_legacy_catalog(source)
    counts = (Product.objects.count(), MediaAsset.objects.count(), CatalogReviewItem.objects.count())
    report = import_legacy_catalog(source)
    assert report.products_created == 0
    assert report.products_unchanged == 7
    assert report.images_stored == 0 and report.images_attached == 0
    assert report.review_items_created == 0 and report.proposals_created == 0
    assert (Product.objects.count(), MediaAsset.objects.count(), CatalogReviewItem.objects.count()) == counts


def test_never_overwrites_edited_products(source, system_actor):
    import_legacy_catalog(source)
    from apps.catalog import services

    product = Product.objects.get(legacy_id=1)
    services.update_product(product, {"name": "Basin Pillar Tap R16 Chrome"}, system_actor)
    report = import_legacy_catalog(source)
    product.refresh_from_db()
    assert product.name == "Basin Pillar Tap R16 Chrome"
    assert report.products_conflicting == [1]


def test_dry_run_changes_nothing_and_writes_no_files(source, settings):
    report = import_legacy_catalog(source, dry_run=True)
    assert report.products_created == 7
    assert report.images_stored == 5
    assert Product.objects.count() == 0
    assert CatalogReviewItem.objects.count() == 0
    media_root = settings.MEDIA_ROOT
    assert not media_root.exists() or not any(media_root.rglob("*.jpg"))


def test_rejects_invalid_rows_and_continues(source):
    bad = CATALOG + [{"id": 50, "sku": "BAD SKU", "cat": "Locks", "sub": "", "name": "Bad"}]
    html = "const CATALOG=" + json.dumps(bad) + ";"
    (source / "index.html").write_text(html, encoding="utf-8")
    report = import_legacy_catalog(source, with_images=False)
    assert report.products_created == 7
    assert report.rejected[0][0] == 50
