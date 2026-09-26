import io
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from openpyxl import Workbook

from apps.catalog import imports, services
from apps.catalog.models import (
    AttributeDefinition,
    AttributeType,
    CatalogChangeProposal,
    ImportStatus,
    Product,
    ProductStatus,
    RowOutcome,
)
from apps.core.actors import Actor
from apps.core.exceptions import Conflict
from apps.pricing import services as pricing
from apps.pricing.models import ProductPrice

from .factories import CategoryFactory, ProductFactory

pytestmark = pytest.mark.django_db

HEADER = [
    "Item Code",
    "Product Name",
    "Category",
    "Sub Category",
    "Brand",
    "UOM",
    "Barcode",
    "Selling Price",
    "attr:diameter_mm",
]


def csv_upload(rows, header=HEADER, name="stock.csv"):
    lines = [",".join(header)] + [",".join(str(c) for c in row) for row in rows]
    return SimpleUploadedFile(name, ("\n".join(lines) + "\n").encode(), content_type="text/csv")


def xlsx_upload(rows, header=HEADER):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(header)
    for row in rows:
        sheet.append(row)
    out = io.BytesIO()
    workbook.save(out)
    return SimpleUploadedFile("stock.xlsx", out.getvalue())


@pytest.fixture
def owner():
    return Actor.system("owner")


@pytest.fixture(autouse=True)
def diameter():
    return AttributeDefinition.objects.create(
        code="diameter_mm", label="Diameter", data_type=AttributeType.NUMBER, unit="mm"
    )


def validated(upload, owner, **flags):
    batch = imports.create_batch(upload, owner, **flags)
    return imports.validate_batch(batch)


def outcomes(batch):
    """{spreadsheet row number: outcome}; the header is row 1."""
    return {r.row_number: r.outcome for r in batch.rows.all()}


class TestValidation:
    def test_report_classifies_rows_without_touching_products(self, owner):
        ProductFactory(sku="EXIST-1", name="Existing Tap")
        rows = [
            ["NEW-1", "PPR Pipe 20mm", "Pipes & Fittings", "PPR", "Kamili", "length", "", "450", "20"],
            ["new-1", "PPR Pipe 20mm again", "Pipes & Fittings", "PPR", "", "", "", "", ""],  # duplicate SKU
            ["", "No SKU item", "Pipes & Fittings", "", "", "", "", "", ""],
            ["NEW-2", "Valve", "", "", "", "", "", "", ""],  # missing category
            ["NEW-3", "Pricey thing", "Tools", "", "", "pcs", "", "2000000", ""],  # suspicious price
            ["NEW-4", "Bad price", "Tools", "", "", "", "", "abc", ""],
            ["NEW-5", "Bad barcode", "Tools", "", "", "", "12AB", "", ""],
            ["NEW-6", "Unknown unit", "Tools", "", "", "furlong", "", "", ""],
            ["EXIST-1", "Existing Tap", "", "", "", "", "", "", ""],  # missing category → rejected
        ]
        batch = validated(csv_upload(rows), owner)
        assert batch.status == ImportStatus.VALIDATED
        assert outcomes(batch) == {
            2: RowOutcome.CREATE,
            3: RowOutcome.DUPLICATE,
            4: RowOutcome.REJECTED,  # no SKU
            5: RowOutcome.REJECTED,  # no category
            6: RowOutcome.CREATE,  # suspicious price is a warning, not an error
            7: RowOutcome.REJECTED,  # unparseable price
            8: RowOutcome.REJECTED,  # bad barcode
            9: RowOutcome.CREATE,  # unknown unit → warning, Piece
            10: RowOutcome.REJECTED,  # existing SKU but no category
        }

        report = batch.report
        assert report["rows_processed"] == 9
        assert report["duplicates"] == 1
        assert report["missing_skus"] == 1
        assert report["missing_categories"] == 2
        assert report["suspicious_values"] >= 1
        assert "Pipes & Fittings / PPR" in report["new_categories"]
        assert report["missing_images"] == report["outcomes"]["New product"]
        assert Product.objects.filter(sku__startswith="NEW").count() == 0  # dry run only

    def test_missing_required_columns_fails_batch(self, owner):
        batch = validated(csv_upload([["x"]], header=["Whatever"]), owner)
        assert batch.status == ImportStatus.FAILED
        assert "Required column" in batch.error

    def test_unsupported_file_type_fails(self, owner):
        upload = SimpleUploadedFile("stock.pdf", b"%PDF-1.4")
        batch = validated(upload, owner)
        assert batch.status == ImportStatus.FAILED

    def test_xlsx_numeric_cells_are_read_as_text(self, owner):
        rows = [[1001, "Numeric SKU item", "Tools", "", "", "", 6161100000017, 250.0, 15]]
        batch = validated(xlsx_upload(rows), owner)
        row = batch.rows.get()
        assert row.outcome == RowOutcome.CREATE, row.errors
        assert row.normalized["sku"] == "1001"
        assert row.normalized["barcode"] == "6161100000017"
        assert row.normalized["price"] == "250.00" or Decimal(row.normalized["price"]) == Decimal("250")

    def test_possible_duplicate_name_warning(self, owner):
        ProductFactory(sku="A-1", name="Chrome Basin Tap")
        batch = validated(csv_upload([["B-1", "chrome basin tap", "Taps", "", "", "", "", "", ""]]), owner)
        assert any("Possible duplicate" in w for w in batch.rows.get().warnings)

    def test_attribute_out_of_scope_is_rejected_at_validation(self, owner, diameter):
        diameter.categories.add(CategoryFactory(name="Pipes"))
        batch = validated(csv_upload([["C-1", "Hammer", "Tools", "", "", "", "", "", "20"]]), owner)
        assert batch.rows.get().outcome == RowOutcome.REJECTED


class TestCommitAndRollback:
    def test_commit_creates_drafts_with_prices_and_attributes(self, owner):
        rows = [["NEW-1", "PPR Pipe 20mm", "Pipes & Fittings", "PPR", "Kamili", "length", "", "450", "20"]]
        batch = validated(csv_upload(rows), owner)
        batch = imports.commit_batch(batch, owner)
        assert batch.status == ImportStatus.COMMITTED
        product = Product.objects.get(sku="NEW-1")
        assert product.status == ProductStatus.DRAFT  # activate_new was off
        assert product.brand.name == "Kamili"
        assert product.unit_of_measure == "LENGTH"
        assert str(product.category) == "Pipes & Fittings / PPR"
        assert product.price_on_request is False
        assert pricing.current_price(product).amount == Decimal("450.00")
        assert product.attribute_values.get().value == Decimal("20")

    def test_commit_is_idempotent(self, owner):
        batch = validated(csv_upload([["NEW-1", "Thing", "Tools", "", "", "", "", "", ""]]), owner)
        imports.commit_batch(batch, owner)
        imports.commit_batch(batch, owner)  # e.g. a retried Celery task
        assert Product.objects.filter(sku="NEW-1").count() == 1

    def test_commit_requires_validation(self, owner):
        batch = imports.create_batch(csv_upload([["NEW-1", "Thing", "Tools", "", "", "", "", "", ""]]), owner)
        with pytest.raises(Conflict):
            imports.commit_batch(batch, owner)

    def test_commit_is_all_or_nothing(self, owner, system_actor):
        rows = [
            ["NEW-1", "Thing one", "Tools", "", "", "", "", "", ""],
            ["NEW-2", "Thing two", "Tools", "", "", "", "", "", ""],
        ]
        batch = validated(csv_upload(rows), owner)
        # Someone creates NEW-2 between validation and approval.
        services.create_product(system_actor, sku="NEW-2", name="Created meanwhile", category=CategoryFactory())
        batch = imports.commit_batch(batch, owner)
        assert batch.status == ImportStatus.FAILED
        assert "Re-validate" in batch.error
        assert not Product.objects.filter(sku="NEW-1").exists()

    def test_existing_products_get_proposals_by_default(self, owner):
        ProductFactory(sku="EXIST-1", name="Old Name", category=CategoryFactory(name="Tools"))
        batch = validated(csv_upload([["EXIST-1", "New Name", "Tools", "", "", "", "", "", ""]]), owner)
        assert batch.rows.get().outcome == RowOutcome.PROPOSE
        imports.commit_batch(batch, owner)
        assert Product.objects.get(sku="EXIST-1").name == "Old Name"
        proposal = CatalogChangeProposal.objects.get()
        assert (proposal.field, proposal.proposed_value) == ("name", "New Name")

    def test_update_existing_then_rollback_restores(self, owner):
        tools = CategoryFactory(name="Tools")
        product = ProductFactory(sku="EXIST-1", name="Old Name", category=tools, price_on_request=False)
        pricing.set_price(product, "100", owner)
        rows = [
            ["EXIST-1", "New Name", "Tools", "", "", "", "", "120", ""],
            ["NEW-9", "Brand new", "Tools", "", "", "", "", "50", ""],
        ]
        batch = validated(csv_upload(rows), owner, update_existing=True)
        imports.commit_batch(batch, owner)
        product.refresh_from_db()
        assert product.name == "New Name"
        assert pricing.current_price(product).amount == Decimal("120")

        result = imports.rollback_batch(batch, owner)
        product.refresh_from_db()
        batch.refresh_from_db()
        assert batch.status == ImportStatus.ROLLED_BACK
        assert product.name == "Old Name"
        assert pricing.current_price(product).amount == Decimal("100")  # previous price reopened
        assert not Product.objects.filter(sku="NEW-9").exists()
        assert result["products_deleted"] == 1 and result["products_restored"] == 1
        assert ProductPrice.objects.filter(product=product).count() == 1

    def test_rollback_skips_rows_edited_after_import(self, owner, system_actor):
        product = ProductFactory(sku="EXIST-1", name="Old", category=CategoryFactory(name="Tools"))
        batch = validated(
            csv_upload([["EXIST-1", "Imported", "Tools", "", "", "", "", "", ""]]), owner, update_existing=True
        )
        imports.commit_batch(batch, owner)
        services.update_product(product, {"name": "Hand edited"}, system_actor)
        result = imports.rollback_batch(batch, owner)
        product.refresh_from_db()
        assert product.name == "Hand edited"
        assert result["skipped"][0]["reason"] == "changed since import"


def test_celery_tasks_run_the_pipeline(owner):
    from apps.catalog import tasks

    batch = imports.create_batch(csv_upload([["T-1", "Task thing", "Tools", "", "", "", "", "", ""]]), owner)
    assert tasks.validate_import_batch.delay(str(batch.pk)).get() == ImportStatus.VALIDATED
    status = tasks.commit_import_batch.delay(str(batch.pk), tasks.actor_payload(owner)).get()
    assert status == ImportStatus.COMMITTED
    assert Product.objects.filter(sku="T-1").exists()
