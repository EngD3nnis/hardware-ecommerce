"""Inventory: an append-only movement ledger plus locked running balances.

Every change to stock is a `StockMovement` with a reason and a source, so
"why is this 43 and not 50?" always has an answer. `StockBalance` holds the
current totals per product and location. Both are written only by
`apps.inventory.services` in the same transaction, under a row lock. A
nightly reconciliation recomputes balances from the ledger and raises an
alert on any mismatch (ADR 0008).

    available = on_hand − reserved        (damaged stock is kept out of on_hand)
    incoming  = open purchase-order quantity not yet received (computed)
"""

from decimal import Decimal

from django.db import models
from django.db.models import F, Q

from apps.catalog.models import Product
from apps.core.actors import ActorType
from apps.core.models import TimeStampedModel

QTY = {"max_digits": 12, "decimal_places": 3}
ZERO = Decimal("0")


class StockLocation(TimeStampedModel):
    code = models.SlugField(max_length=30, unique=True)
    name = models.CharField(max_length=100)
    is_default = models.BooleanField(default=False, help_text="Used when no location is specified.")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-is_default", "name"]
        constraints = [
            models.UniqueConstraint(fields=["is_default"], condition=Q(is_default=True), name="one_default_location")
        ]

    def __str__(self):
        return self.name


class StockBalance(models.Model):
    id = models.BigAutoField(primary_key=True)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="stock_balances")
    location = models.ForeignKey(StockLocation, on_delete=models.PROTECT, related_name="balances")
    on_hand = models.DecimalField(**QTY, default=ZERO, help_text="Physically present and sellable.")
    reserved = models.DecimalField(**QTY, default=ZERO, help_text="Promised to orders, not yet dispatched.")
    damaged = models.DecimalField(**QTY, default=ZERO, help_text="Physically present, not sellable.")
    reorder_point = models.DecimalField(
        **QTY, null=True, blank=True, help_text="Alert when available stock falls to this level."
    )
    reorder_quantity = models.DecimalField(**QTY, null=True, blank=True, help_text="Suggested quantity to reorder.")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["product", "location"], name="stock_balance_unique"),
            models.CheckConstraint(condition=Q(on_hand__gte=0), name="stock_on_hand_not_negative"),
            models.CheckConstraint(condition=Q(reserved__gte=0), name="stock_reserved_not_negative"),
            models.CheckConstraint(condition=Q(damaged__gte=0), name="stock_damaged_not_negative"),
            models.CheckConstraint(condition=Q(reserved__lte=F("on_hand")), name="stock_reserved_within_on_hand"),
        ]

    def __str__(self):
        return f"{self.product.sku} @ {self.location.code}: {self.on_hand}"

    @property
    def available(self) -> Decimal:
        return self.on_hand - self.reserved


class Bucket(models.TextChoices):
    ON_HAND = "ON_HAND", "On hand"
    DAMAGED = "DAMAGED", "Damaged"


class MovementReason(models.TextChoices):
    PURCHASE_RECEIVED = "PURCHASE_RECEIVED", "Purchase received"
    SALE = "SALE", "Sale / dispatch"
    RETURN = "RETURN", "Customer return"
    DAMAGE = "DAMAGE", "Marked damaged"
    DAMAGE_WRITE_OFF = "DAMAGE_WRITE_OFF", "Damaged stock written off"
    DAMAGE_RECOVERED = "DAMAGE_RECOVERED", "Damaged stock recovered"
    TRANSFER_IN = "TRANSFER_IN", "Transfer in"
    TRANSFER_OUT = "TRANSFER_OUT", "Transfer out"
    MANUAL_ADJUSTMENT = "MANUAL_ADJUSTMENT", "Manual adjustment"
    STOCKTAKE_CORRECTION = "STOCKTAKE_CORRECTION", "Stock count correction"
    OPENING_BALANCE = "OPENING_BALANCE", "Opening balance"


class StockMovement(models.Model):
    """One signed change to one bucket of one balance. Append-only (DB trigger)."""

    id = models.BigAutoField(primary_key=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="stock_movements")
    location = models.ForeignKey(StockLocation, on_delete=models.PROTECT, related_name="movements")
    bucket = models.CharField(max_length=10, choices=Bucket.choices, default=Bucket.ON_HAND)
    quantity = models.DecimalField(**QTY, help_text="Signed: positive adds stock, negative removes it.")
    balance_after = models.DecimalField(**QTY, help_text="The bucket's total after this movement.")
    reason = models.CharField(max_length=24, choices=MovementReason.choices)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    # What caused it, e.g. ("procurement.goodsreceipt", "<uuid>") or ("orders.order", "<uuid>").
    source_type = models.CharField(max_length=60, blank=True)
    source_id = models.CharField(max_length=64, blank=True)
    note = models.CharField(max_length=255, blank=True)
    actor_type = models.CharField(max_length=16, choices=ActorType.choices)
    actor_label = models.CharField(max_length=255)
    correlation_id = models.CharField(max_length=128, blank=True)
    idempotency_key = models.CharField(max_length=150, unique=True, null=True, blank=True)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["product", "location", "id"]),
            models.Index(fields=["source_type", "source_id"]),
        ]
        constraints = [models.CheckConstraint(condition=~Q(quantity=0), name="stock_movement_not_zero")]

    def __str__(self):
        sign = "+" if self.quantity > 0 else ""
        return f"{self.product.sku} {sign}{self.quantity} {self.get_reason_display()}"


class ReservationStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    RELEASED = "RELEASED", "Released"
    CONSUMED = "CONSUMED", "Consumed (dispatched)"


class Reservation(TimeStampedModel):
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="reservations")
    location = models.ForeignKey(StockLocation, on_delete=models.PROTECT, related_name="reservations")
    quantity = models.DecimalField(**QTY)
    status = models.CharField(max_length=10, choices=ReservationStatus.choices, default=ReservationStatus.ACTIVE)
    source_type = models.CharField(max_length=60)
    source_id = models.CharField(max_length=64)
    expires_at = models.DateTimeField(null=True, blank=True, help_text="Released automatically after this time.")
    closed_at = models.DateTimeField(null=True, blank=True)
    close_reason = models.CharField(max_length=255, blank=True)
    idempotency_key = models.CharField(max_length=150, unique=True, null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["status", "expires_at"]),
            models.Index(fields=["source_type", "source_id"]),
        ]
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0), name="reservation_quantity_positive")]

    def __str__(self):
        return f"{self.quantity} × {self.product.sku} for {self.source_type}:{self.source_id} ({self.status})"


class AdjustmentKind(models.TextChoices):
    OPENING_BALANCE = "OPENING_BALANCE", "Opening balance (add)"
    FOUND = "FOUND", "Found / add stock"
    LOST = "LOST", "Lost / remove stock"
    DAMAGE = "DAMAGE", "Mark as damaged"
    DAMAGE_WRITE_OFF = "DAMAGE_WRITE_OFF", "Write off damaged stock"
    DAMAGE_RECOVERED = "DAMAGE_RECOVERED", "Damaged stock is sellable again"
    RETURN = "RETURN", "Customer return (sellable)"
    RETURN_DAMAGED = "RETURN_DAMAGED", "Customer return (damaged)"


class StockAdjustment(TimeStampedModel):
    """A person's manual stock change: the human override for inventory.

    Created in the admin; saving it calls the inventory service, which writes
    the movement(s). Read-only afterwards.
    """

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="+")
    location = models.ForeignKey(StockLocation, on_delete=models.PROTECT, related_name="+")
    kind = models.CharField(max_length=20, choices=AdjustmentKind.choices)
    quantity = models.DecimalField(**QTY, help_text="Always positive; the kind decides the direction.")
    note = models.CharField(max_length=255, help_text="Why? Required for accountability.")
    created_by = models.CharField(max_length=255, blank=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(quantity__gt=0), name="adjustment_quantity_positive")]

    def __str__(self):
        return f"{self.get_kind_display()}: {self.quantity} × {self.product.sku}"


class CountStatus(models.TextChoices):
    OPEN = "OPEN", "Counting"
    COMPLETED = "COMPLETED", "Completed (corrections applied)"
    CANCELLED = "CANCELLED", "Cancelled"


class StockCount(TimeStampedModel):
    """Stocktake: record physical counts, then apply the differences as corrections."""

    location = models.ForeignKey(StockLocation, on_delete=models.PROTECT, related_name="counts")
    status = models.CharField(max_length=10, choices=CountStatus.choices, default=CountStatus.OPEN)
    note = models.CharField(max_length=255, blank=True)
    started_by = models.CharField(max_length=255, blank=True)
    completed_by = models.CharField(max_length=255, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        permissions = [("complete_stockcount", "Can complete a stock count (applies corrections)")]

    def __str__(self):
        return f"Count at {self.location} ({self.get_status_display()}) {self.created_at:%Y-%m-%d}"


class StockCountLine(models.Model):
    id = models.BigAutoField(primary_key=True)
    count = models.ForeignKey(StockCount, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="+")
    counted_quantity = models.DecimalField(**QTY, null=True, blank=True, help_text="Leave empty if not counted.")
    system_quantity = models.DecimalField(**QTY, null=True, blank=True, help_text="On hand when the count completed.")
    variance = models.DecimalField(**QTY, null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["count", "product"], name="count_line_unique_product"),
            models.CheckConstraint(
                condition=Q(counted_quantity__isnull=True) | Q(counted_quantity__gte=0), name="count_not_negative"
            ),
        ]

    def __str__(self):
        return f"{self.product.sku}: {self.counted_quantity}"
