"""Suppliers, purchase orders and goods receipts.

Lifecycle of a purchase order (see services.TRANSITIONS):

    DRAFT → PENDING_APPROVAL → APPROVED → SENT → PARTIALLY_RECEIVED → RECEIVED
      │            │              │        │
      └────────────┴──────────────┴────────┴──→ CANCELLED (only before anything is received)

Receiving goods creates a GoodsReceipt and PURCHASE_RECEIVED stock movements
through the inventory service.
"""

from decimal import Decimal

from django.db import models
from django.db.models import F, Q

from apps.catalog.models import Product
from apps.core.models import TimeStampedModel
from apps.inventory.models import QTY, StockLocation

MONEY = {"max_digits": 12, "decimal_places": 2}


class Supplier(TimeStampedModel):
    name = models.CharField(max_length=150, unique=True)
    code = models.SlugField(max_length=30, unique=True)
    contact_person = models.CharField(max_length=100, blank=True)
    phone = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    kra_pin = models.CharField("KRA PIN", max_length=20, blank=True)
    payment_terms_days = models.PositiveSmallIntegerField(default=0, help_text="0 = cash on delivery.")
    lead_time_days = models.PositiveSmallIntegerField(default=7, help_text="Typical days from order to delivery.")
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class SupplierProduct(TimeStampedModel):
    """What a supplier sells us: their code, our last cost, lead time."""

    supplier = models.ForeignKey(Supplier, on_delete=models.CASCADE, related_name="products")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="supplier_products")
    supplier_sku = models.CharField(max_length=64, blank=True)
    last_cost = models.DecimalField(**MONEY, null=True, blank=True)
    lead_time_days = models.PositiveSmallIntegerField(null=True, blank=True, help_text="Overrides the supplier's.")
    min_order_quantity = models.DecimalField(**QTY, null=True, blank=True)
    is_preferred = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["supplier", "product"], name="supplier_product_unique"),
            models.UniqueConstraint(
                fields=["supplier", "supplier_sku"], condition=~Q(supplier_sku=""), name="supplier_sku_unique"
            ),
            models.UniqueConstraint(
                fields=["product"], condition=Q(is_preferred=True), name="one_preferred_supplier_per_product"
            ),
        ]

    def __str__(self):
        return f"{self.supplier} → {self.product.sku}"


class POStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    PENDING_APPROVAL = "PENDING_APPROVAL", "Waiting for approval"
    APPROVED = "APPROVED", "Approved"
    SENT = "SENT", "Sent to supplier"
    PARTIALLY_RECEIVED = "PARTIALLY_RECEIVED", "Partially received"
    RECEIVED = "RECEIVED", "Received"
    CANCELLED = "CANCELLED", "Cancelled"


OPEN_PO_STATUSES = (POStatus.APPROVED, POStatus.SENT, POStatus.PARTIALLY_RECEIVED)


class PurchaseOrder(TimeStampedModel):
    number = models.CharField(max_length=20, unique=True)
    supplier = models.ForeignKey(Supplier, on_delete=models.PROTECT, related_name="purchase_orders")
    location = models.ForeignKey(StockLocation, on_delete=models.PROTECT, related_name="+", help_text="Deliver to.")
    status = models.CharField(max_length=20, choices=POStatus.choices, default=POStatus.DRAFT)
    expected_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    created_by = models.CharField(max_length=255)
    created_by_agent = models.BooleanField(default=False)
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.CharField(max_length=255, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]
        permissions = [("approve_purchaseorder", "Can approve purchase orders")]

    def __str__(self):
        return f"{self.number} {self.supplier}"

    @property
    def total(self) -> Decimal:
        return sum((line.line_total for line in self.lines.all()), Decimal("0"))


class PurchaseOrderLine(models.Model):
    id = models.BigAutoField(primary_key=True)
    order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="+")
    quantity = models.DecimalField(**QTY)
    unit_cost = models.DecimalField(**MONEY)
    received_quantity = models.DecimalField(**QTY, default=Decimal("0"))

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["order", "product"], name="po_line_unique_product"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="po_line_quantity_positive"),
            models.CheckConstraint(condition=Q(unit_cost__gte=0), name="po_line_cost_not_negative"),
            models.CheckConstraint(
                condition=Q(received_quantity__gte=0) & Q(received_quantity__lte=F("quantity")),
                name="po_line_received_within_ordered",
            ),
        ]

    def __str__(self):
        return f"{self.quantity} × {self.product.sku}"

    @property
    def line_total(self) -> Decimal:
        return (self.quantity * self.unit_cost).quantize(Decimal("0.01"))

    @property
    def outstanding(self) -> Decimal:
        return self.quantity - self.received_quantity


class GoodsReceipt(TimeStampedModel):
    order = models.ForeignKey(PurchaseOrder, on_delete=models.PROTECT, related_name="receipts")
    received_by = models.CharField(max_length=255)
    note = models.CharField(max_length=255, blank=True)
    idempotency_key = models.CharField(max_length=150, unique=True, null=True, blank=True)

    def __str__(self):
        return f"Receipt for {self.order.number} at {self.created_at:%Y-%m-%d %H:%M}"


class GoodsReceiptLine(models.Model):
    id = models.BigAutoField(primary_key=True)
    receipt = models.ForeignKey(GoodsReceipt, on_delete=models.CASCADE, related_name="lines")
    order_line = models.ForeignKey(PurchaseOrderLine, on_delete=models.PROTECT, related_name="receipt_lines")
    quantity = models.DecimalField(**QTY)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0), name="receipt_line_quantity_positive")]

    def __str__(self):
        return f"{self.quantity} × {self.order_line.product.sku}"
