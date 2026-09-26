"""Canonical product catalogue.

Design (docs/architecture/target-state.md §3.1, ADR 0006):
- `Product` is the sellable item and owns the SKU. Optional `ProductFamily`
  groups variants (e.g. one tap in several finishes) without forcing a
  separate variant table on the ~1,000 single-SKU products.
- Common hardware properties are real columns. Category-specific specs
  (PPR pressure class, lock cylinder type…) are typed `AttributeDefinition`s
  that staff create in admin, with no migration needed.
- Images are content-addressed `MediaAsset`s (one stored file per unique
  image) linked to products through `ProductMedia`.

Write through `apps.catalog.services`, not directly: services validate,
normalise and write the audit log.
"""

import re
from decimal import Decimal

from django.db import models
from django.db.models import Q
from django.db.models.functions import Upper

from apps.core.models import TimeStampedModel

SKU_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._/-]{0,63}$")


def normalise_sku(value: str) -> str:
    """SKUs are case-insensitive identifiers; store them upper-case, trimmed."""
    return (value or "").strip().upper()


class Category(TimeStampedModel):
    """Two levels in practice: category → subcategory (the tree allows more)."""

    name = models.CharField(max_length=150)
    slug = models.SlugField(max_length=180, unique=True)
    parent = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="children")
    description = models.TextField(blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = "categories"
        ordering = ["sort_order", "name"]
        constraints = [
            models.UniqueConstraint(fields=["parent", "name"], name="category_unique_name_per_parent"),
            models.UniqueConstraint(
                fields=["name"], condition=Q(parent__isnull=True), name="category_unique_top_level_name"
            ),
        ]

    def __str__(self):
        return f"{self.parent.name} / {self.name}" if self.parent_id else self.name


class Brand(TimeStampedModel):
    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=140, unique=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class ProductFamily(TimeStampedModel):
    """Optional grouping of products that differ only by attributes (size, finish…)."""

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=280, unique=True)
    brand = models.ForeignKey(Brand, on_delete=models.PROTECT, null=True, blank=True, related_name="families")
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="families")
    description = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "product families"
        ordering = ["name"]

    def __str__(self):
        return self.name


class UnitOfMeasure(models.TextChoices):
    PIECE = "PIECE", "Piece"
    PAIR = "PAIR", "Pair"
    SET = "SET", "Set"
    METRE = "METRE", "Metre"
    LENGTH = "LENGTH", "Length (e.g. 6 m pipe)"
    ROLL = "ROLL", "Roll"
    SHEET = "SHEET", "Sheet"
    KG = "KG", "Kilogram"
    BAG = "BAG", "Bag"
    LITRE = "LITRE", "Litre"
    TIN = "TIN", "Tin"
    BOX = "BOX", "Box"
    PACKET = "PACKET", "Packet"


class ProductStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft (not visible to customers)"
    ACTIVE = "ACTIVE", "Active"
    ARCHIVED = "ARCHIVED", "Archived (no longer sold)"


class Product(TimeStampedModel):
    sku = models.CharField(max_length=64, unique=True, help_text="Internal SKU, e.g. DWX-0001. Upper-case.")
    legacy_id = models.PositiveIntegerField(
        unique=True,
        null=True,
        blank=True,
        help_text="Id in the original static website. Old WhatsApp quote links use it; never reuse one.",
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=300, unique=True)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=16, choices=ProductStatus.choices, default=ProductStatus.DRAFT)

    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="products")
    brand = models.ForeignKey(Brand, on_delete=models.PROTECT, null=True, blank=True, related_name="products")
    family = models.ForeignKey(ProductFamily, on_delete=models.SET_NULL, null=True, blank=True, related_name="products")

    unit_of_measure = models.CharField(max_length=16, choices=UnitOfMeasure.choices, default=UnitOfMeasure.PIECE)
    # Common structured properties (see module docstring).
    material = models.CharField(max_length=100, blank=True)
    finish = models.CharField(max_length=100, blank=True)
    colour = models.CharField(max_length=60, blank=True)
    size_label = models.CharField(max_length=60, blank=True, help_text='Free text size, e.g. "1/2 inch", "20mm".')
    length_mm = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    width_mm = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    height_mm = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    weight_g = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    barcode = models.CharField(max_length=32, unique=True, null=True, blank=True, help_text="GTIN/EAN if known.")
    price_on_request = models.BooleanField(
        default=True,
        help_text="Always quote this item individually, even if a list price exists.",
    )

    class Meta:
        ordering = ["name"]
        constraints = [
            models.CheckConstraint(condition=Q(sku=Upper("sku")), name="product_sku_upper_case"),
            models.CheckConstraint(condition=~Q(sku=""), name="product_sku_not_empty"),
        ]
        indexes = [models.Index(fields=["status", "category"])]

    def __str__(self):
        return f"{self.sku} {self.name}"

    @property
    def is_sellable(self) -> bool:
        return self.status == ProductStatus.ACTIVE


class IdentifierKind(models.TextChoices):
    LEGACY_SKU = "LEGACY_SKU", "Legacy SKU"
    ALIAS_SKU = "ALIAS_SKU", "Alternative SKU"
    GTIN = "GTIN", "GTIN / EAN (additional)"
    MANUFACTURER_PART = "MANUFACTURER_PART", "Manufacturer part number"


class ProductIdentifier(TimeStampedModel):
    """Other codes that identify a product. Supplier SKUs live on SupplierProduct."""

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="identifiers")
    kind = models.CharField(max_length=24, choices=IdentifierKind.choices)
    value = models.CharField(max_length=100)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["kind", "value"], name="identifier_unique_per_kind"),
            models.CheckConstraint(condition=Q(value=Upper("value")), name="identifier_value_upper_case"),
        ]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.value}"


class ProductAlias(TimeStampedModel):
    """Extra search terms for one product (trade names, Swahili names, misspellings)."""

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="aliases")
    text = models.CharField(max_length=150)

    class Meta:
        verbose_name_plural = "product aliases"
        constraints = [models.UniqueConstraint(Upper("text"), "product", name="alias_unique_per_product")]

    def __str__(self):
        return self.text


class AttributeType(models.TextChoices):
    TEXT = "TEXT", "Text"
    NUMBER = "NUMBER", "Number"
    CHOICE = "CHOICE", "Choice from a list"
    BOOLEAN = "BOOLEAN", "Yes / no"


class AttributeDefinition(TimeStampedModel):
    """A typed specification that products can have, e.g. "diameter_mm" (NUMBER, mm)."""

    code = models.SlugField(max_length=60, unique=True, help_text="Stable machine name, e.g. diameter_mm.")
    label = models.CharField(max_length=100)
    data_type = models.CharField(max_length=10, choices=AttributeType.choices)
    unit = models.CharField(max_length=20, blank=True, help_text="Display unit for NUMBER, e.g. mm, bar, L.")
    choices = models.JSONField(default=list, blank=True, help_text='For CHOICE: a JSON list, e.g. ["PN16","PN20"].')
    categories = models.ManyToManyField(
        Category,
        blank=True,
        related_name="attribute_definitions",
        help_text="Where this attribute applies. Empty = all categories.",
    )
    is_filterable = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "label"]

    def __str__(self):
        return f"{self.label} ({self.unit})" if self.unit else self.label


class ProductAttributeValue(TimeStampedModel):
    """Exactly one of the value columns is set, matching the definition's type."""

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="attribute_values")
    attribute = models.ForeignKey(AttributeDefinition, on_delete=models.PROTECT, related_name="values")
    # NULL (not "") means "not this type"; the exactly-one-value constraint relies on it.
    value_text = models.CharField(max_length=255, null=True, blank=True)  # noqa: DJ001
    value_number = models.DecimalField(max_digits=14, decimal_places=4, null=True, blank=True)
    value_bool = models.BooleanField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["product", "attribute"], name="attribute_value_unique_per_product"),
            models.CheckConstraint(
                condition=(
                    Q(value_text__isnull=False, value_number__isnull=True, value_bool__isnull=True)
                    | Q(value_text__isnull=True, value_number__isnull=False, value_bool__isnull=True)
                    | Q(value_text__isnull=True, value_number__isnull=True, value_bool__isnull=False)
                ),
                name="attribute_value_exactly_one",
            ),
        ]

    @property
    def value(self):
        if self.value_number is not None:
            # 20.0000 → 20, 12.5000 → 12.5 (normalize() alone would give 2E+1)
            return Decimal(format(self.value_number.normalize(), "f"))
        if self.value_bool is not None:
            return self.value_bool
        return self.value_text

    def __str__(self):
        unit = f" {self.attribute.unit}" if self.attribute.unit else ""
        return f"{self.attribute.label}: {self.value}{unit}"


def media_upload_path(instance: "MediaAsset", filename: str) -> str:
    # Content-addressed: the same image is stored once, whatever it is called.
    return f"products/{instance.sha256[:2]}/{instance.sha256}.{instance.extension}"


class MediaAsset(TimeStampedModel):
    """One stored image file, identified by the SHA-256 of its bytes."""

    sha256 = models.CharField(max_length=64, unique=True)
    file = models.FileField(upload_to=media_upload_path, max_length=255)
    content_type = models.CharField(max_length=50)
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    byte_size = models.PositiveIntegerField()
    original_filename = models.CharField(max_length=255, blank=True)

    EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}

    @property
    def extension(self) -> str:
        return self.EXTENSIONS[self.content_type]

    def __str__(self):
        return f"{self.original_filename or self.sha256[:12]} ({self.width}×{self.height})"


class ProductMedia(TimeStampedModel):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="media")
    asset = models.ForeignKey(MediaAsset, on_delete=models.PROTECT, related_name="usages")
    alt_text = models.CharField(max_length=255, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_primary = models.BooleanField(default=False)

    class Meta:
        verbose_name_plural = "product media"
        ordering = ["-is_primary", "sort_order"]
        constraints = [
            models.UniqueConstraint(fields=["product", "asset"], name="media_asset_once_per_product"),
            models.UniqueConstraint(
                fields=["product"], condition=Q(is_primary=True), name="media_one_primary_per_product"
            ),
        ]

    def __str__(self):
        return f"{self.product.sku} image {self.sort_order}"


class ProductDocument(TimeStampedModel):
    """Datasheets, manuals, certificates."""

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="documents")
    title = models.CharField(max_length=200)
    file = models.FileField(upload_to="product-documents/%Y/%m/", max_length=255)

    def __str__(self):
        return self.title


# --- Data-quality review -----------------------------------------------------


class ReviewKind(models.TextChoices):
    DUPLICATE_IMAGE = "DUPLICATE_IMAGE", "Same image on several products"
    DUPLICATE_NAME = "DUPLICATE_NAME", "Several products with the same name"
    TRUNCATED_NAME = "TRUNCATED_NAME", "Name looks cut off"
    MISSING_IMAGE = "MISSING_IMAGE", "No image"
    GENERIC_CATEGORY = "GENERIC_CATEGORY", "Catch-all subcategory"
    SUSPICIOUS_VALUE = "SUSPICIOUS_VALUE", "Suspicious value"
    OTHER = "OTHER", "Other"


class ReviewStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    RESOLVED = "RESOLVED", "Resolved"
    DISMISSED = "DISMISSED", "Dismissed (not a problem)"


class CatalogReviewItem(TimeStampedModel):
    """A catalogue data-quality problem for a human to look at.

    `fingerprint` makes detection idempotent: re-running an import or check
    does not reopen or duplicate an item that already exists.
    """

    kind = models.CharField(max_length=24, choices=ReviewKind.choices)
    status = models.CharField(max_length=12, choices=ReviewStatus.choices, default=ReviewStatus.OPEN)
    fingerprint = models.CharField(max_length=200, unique=True)
    summary = models.CharField(max_length=300)
    products = models.ManyToManyField(Product, blank=True, related_name="review_items")
    details = models.JSONField(default=dict, blank=True)
    source = models.CharField(max_length=60, blank=True, help_text="What detected it, e.g. legacy_import.")
    resolved_by = models.CharField(max_length=255, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolution_note = models.TextField(blank=True)

    class Meta:
        ordering = ["status", "kind", "-created_at"]

    def __str__(self):
        return self.summary


class ProposalStatus(models.TextChoices):
    PENDING = "PENDING", "Pending review"
    APPLIED = "APPLIED", "Approved and applied"
    REJECTED = "REJECTED", "Rejected"
    STALE = "STALE", "Stale (value changed since proposed)"


class CatalogChangeProposal(TimeStampedModel):
    """A suggested change to one product field, applied only after human approval.

    Used for importer suggestions and (Stage 9) the Catalogue Agent, which may
    never edit products directly.
    """

    PROPOSABLE_FIELDS = (
        "name",
        "description",
        "category",
        "brand",
        "unit_of_measure",
        "material",
        "finish",
        "colour",
        "size_label",
        "barcode",
    )

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="change_proposals")
    field = models.CharField(max_length=40)
    current_value = models.JSONField(null=True, blank=True)
    proposed_value = models.JSONField(null=True, blank=True)
    reason = models.TextField(blank=True)
    source = models.CharField(max_length=60, help_text="e.g. legacy_import, catalogue_agent")
    proposed_by = models.CharField(max_length=255)
    status = models.CharField(max_length=10, choices=ProposalStatus.choices, default=ProposalStatus.PENDING)
    decided_by = models.CharField(max_length=255, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)

    class Meta:
        ordering = ["status", "-created_at"]
        permissions = [("decide_catalogchangeproposal", "Can approve or reject catalogue change proposals")]
        constraints = [
            models.UniqueConstraint(
                fields=["product", "field", "source"],
                condition=Q(status="PENDING"),
                name="one_pending_proposal_per_field_and_source",
            )
        ]

    def __str__(self):
        return f"{self.product.sku}.{self.field} → {self.proposed_value!r}"


# --- Spreadsheet imports -----------------------------------------------------------


class ImportStatus(models.TextChoices):
    UPLOADED = "UPLOADED", "Uploaded"
    VALIDATING = "VALIDATING", "Validating"
    VALIDATED = "VALIDATED", "Validated: review the report, then approve"
    COMMITTING = "COMMITTING", "Importing"
    COMMITTED = "COMMITTED", "Imported"
    FAILED = "FAILED", "Failed"
    ROLLED_BACK = "ROLLED_BACK", "Rolled back"


class ImportBatch(TimeStampedModel):
    """One uploaded spreadsheet moving through validate → approve → commit (→ rollback).

    Nothing touches products until a person approves a validated batch, and
    the dry-run report shows exactly what would happen first.
    """

    file = models.FileField(upload_to="imports/%Y/%m/", max_length=255)
    original_filename = models.CharField(max_length=255, blank=True)
    file_sha256 = models.CharField(max_length=64, blank=True, db_index=True)
    status = models.CharField(max_length=12, choices=ImportStatus.choices, default=ImportStatus.UPLOADED)
    update_existing = models.BooleanField(
        default=False,
        help_text="Apply changes to existing products. Off: differences become proposals for review instead.",
    )
    activate_new = models.BooleanField(
        default=False, help_text="Publish new products immediately. Off: they are created as drafts."
    )
    report = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    created_by = models.CharField(max_length=255, blank=True)
    approved_by = models.CharField(max_length=255, blank=True)
    committed_at = models.DateTimeField(null=True, blank=True)
    rolled_back_by = models.CharField(max_length=255, blank=True)
    rolled_back_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name_plural = "import batches"
        ordering = ["-created_at"]
        permissions = [
            ("commit_importbatch", "Can approve and import a validated spreadsheet"),
            ("rollback_importbatch", "Can roll back an imported spreadsheet"),
        ]

    def __str__(self):
        return f"Import {self.original_filename or self.pk} ({self.get_status_display()})"


class RowOutcome(models.TextChoices):
    CREATE = "CREATE", "New product"
    UPDATE = "UPDATE", "Update existing product"
    PROPOSE = "PROPOSE", "Differences proposed for review"
    UNCHANGED = "UNCHANGED", "No change"
    REJECTED = "REJECTED", "Rejected"
    DUPLICATE = "DUPLICATE", "Duplicate SKU in this file"


class ImportRow(models.Model):
    id = models.BigAutoField(primary_key=True)
    batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="rows")
    row_number = models.PositiveIntegerField(help_text="Spreadsheet row number (header is row 1).")
    raw = models.JSONField(default=dict)
    normalized = models.JSONField(default=dict)
    outcome = models.CharField(max_length=10, choices=RowOutcome.choices)
    errors = models.JSONField(default=list, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    # Filled in at commit, used by rollback.
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    applied = models.BooleanField(default=False)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    created_price_id = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ["batch", "row_number"]
        constraints = [models.UniqueConstraint(fields=["batch", "row_number"], name="import_row_unique_number")]

    def __str__(self):
        return f"Row {self.row_number}: {self.get_outcome_display()}"
