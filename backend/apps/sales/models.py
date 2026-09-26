"""Quotations and orders.

A quotation is Dewmix's normal first step (the WhatsApp "please quote me"
flow). An accepted quotation converts into an order with one call; the lines
are copied as snapshots, so later catalogue or price edits never change a
past quote or order (ADR 0009).

Order state has two independent dimensions:
- `status`: where the goods are (DRAFT → CONFIRMED → ALLOCATED → PICKING →
  PACKED → DISPATCHED → DELIVERED, or CANCELLED). Legal moves are listed in
  services.ORDER_TRANSITIONS.
- `payment_status`: where the money is (UNPAID, PENDING, PARTIALLY_PAID,
  PAID, PARTIALLY_REFUNDED, REFUNDED). It is derived from payments, never set by hand.
"""

from decimal import Decimal

from django.db import models
from django.db.models import Q

from apps.catalog.models import Product
from apps.core.actors import ActorType
from apps.core.models import TimeStampedModel
from apps.customers.models import Customer
from apps.inventory.models import QTY

MONEY = {"max_digits": 12, "decimal_places": 2}
ZERO = Decimal("0.00")


class Channel(models.TextChoices):
    WEBSITE = "WEBSITE", "Website"
    WHATSAPP = "WHATSAPP", "WhatsApp"
    WALK_IN = "WALK_IN", "Walk-in / counter"
    PHONE = "PHONE", "Phone"
    AGENT = "AGENT", "AI sales agent"


class QuoteStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    SENT = "SENT", "Sent to customer"
    ACCEPTED = "ACCEPTED", "Accepted"
    REJECTED = "REJECTED", "Rejected"
    EXPIRED = "EXPIRED", "Expired"
    CONVERTED = "CONVERTED", "Converted to order"


class Quotation(TimeStampedModel):
    number = models.CharField(max_length=20, unique=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="quotations", null=True, blank=True)
    status = models.CharField(max_length=10, choices=QuoteStatus.choices, default=QuoteStatus.DRAFT)
    channel = models.CharField(max_length=10, choices=Channel.choices, default=Channel.WALK_IN)
    valid_until = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True, help_text="Shown to the customer.")
    internal_notes = models.TextField(blank=True)
    prices_include_vat = models.BooleanField(default=True)
    created_by = models.CharField(max_length=255)
    created_by_agent = models.BooleanField(default=False)
    sent_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=150, unique=True, null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.number

    @property
    def is_fully_priced(self) -> bool:
        return all(line.unit_price is not None for line in self.lines.all())

    @property
    def total(self) -> Decimal:
        return sum((line.line_total or ZERO for line in self.lines.all()), ZERO)


class LineSnapshot(models.Model):
    """What the customer saw at the time: never recomputed from the live catalogue."""

    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="+")
    sku = models.CharField(max_length=64)
    name = models.CharField(max_length=255)
    unit_of_measure = models.CharField(max_length=16)
    attributes = models.JSONField(default=dict, blank=True)
    quantity = models.DecimalField(**QTY)
    unit_price = models.DecimalField(**MONEY, null=True, blank=True)
    discount = models.DecimalField(**MONEY, default=ZERO, help_text="Total discount for this line.")

    class Meta:
        abstract = True

    @property
    def line_total(self) -> Decimal | None:
        if self.unit_price is None:
            return None
        return (self.quantity * self.unit_price - self.discount).quantize(Decimal("0.01"))


class QuotationLine(LineSnapshot):
    id = models.BigAutoField(primary_key=True)
    quotation = models.ForeignKey(Quotation, on_delete=models.CASCADE, related_name="lines")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["quotation", "product"], name="quote_line_unique_product"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="quote_line_quantity_positive"),
            models.CheckConstraint(condition=Q(unit_price__isnull=True) | Q(unit_price__gte=0), name="quote_price_ok"),
            models.CheckConstraint(condition=Q(discount__gte=0), name="quote_discount_not_negative"),
        ]

    def __str__(self):
        return f"{self.quantity} × {self.sku}"


class OrderStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    CONFIRMED = "CONFIRMED", "Confirmed"
    ALLOCATED = "ALLOCATED", "Stock allocated"
    PICKING = "PICKING", "Picking"
    PACKED = "PACKED", "Packed"
    DISPATCHED = "DISPATCHED", "Dispatched / collected"
    DELIVERED = "DELIVERED", "Delivered"
    CANCELLED = "CANCELLED", "Cancelled"


class PaymentStatus(models.TextChoices):
    UNPAID = "UNPAID", "Unpaid"
    PENDING = "PENDING", "Payment in progress"
    PARTIALLY_PAID = "PARTIALLY_PAID", "Partially paid"
    PAID = "PAID", "Paid"
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED", "Partially refunded"
    REFUNDED = "REFUNDED", "Refunded"


class DeliveryMethod(models.TextChoices):
    PICKUP = "PICKUP", "Customer collects"
    DELIVERY = "DELIVERY", "We deliver"


class Order(TimeStampedModel):
    number = models.CharField(max_length=20, unique=True)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="orders")
    quotation = models.OneToOneField(
        Quotation,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="order",
        help_text="A quotation converts into at most one order.",
    )
    status = models.CharField(max_length=12, choices=OrderStatus.choices, default=OrderStatus.DRAFT)
    payment_status = models.CharField(max_length=20, choices=PaymentStatus.choices, default=PaymentStatus.UNPAID)
    channel = models.CharField(max_length=10, choices=Channel.choices, default=Channel.WALK_IN)
    delivery_method = models.CharField(max_length=10, choices=DeliveryMethod.choices, default=DeliveryMethod.PICKUP)
    delivery_address = models.TextField(blank=True)
    prices_include_vat = models.BooleanField(default=True)
    total = models.DecimalField(**MONEY, default=ZERO, help_text="Sum of line totals when confirmed.")
    amount_paid = models.DecimalField(**MONEY, default=ZERO)
    amount_refunded = models.DecimalField(**MONEY, default=ZERO)
    notes = models.TextField(blank=True)
    created_by = models.CharField(max_length=255)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    allocated_at = models.DateTimeField(null=True, blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancel_reason = models.CharField(max_length=255, blank=True)
    idempotency_key = models.CharField(max_length=150, unique=True, null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        permissions = [("cancel_order", "Can cancel orders")]
        constraints = [
            models.CheckConstraint(condition=Q(total__gte=0), name="order_total_not_negative"),
            models.CheckConstraint(condition=Q(amount_paid__gte=0), name="order_paid_not_negative"),
            models.CheckConstraint(condition=Q(amount_refunded__gte=0), name="order_refunded_not_negative"),
            models.CheckConstraint(
                condition=Q(amount_refunded__lte=models.F("amount_paid")), name="order_refund_within_paid"
            ),
        ]

    def __str__(self):
        return self.number

    @property
    def balance_due(self) -> Decimal:
        return self.total - self.amount_paid + self.amount_refunded


class OrderLine(LineSnapshot):
    id = models.BigAutoField(primary_key=True)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="lines")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["order", "product"], name="order_line_unique_product"),
            models.CheckConstraint(condition=Q(quantity__gt=0), name="order_line_quantity_positive"),
            models.CheckConstraint(condition=Q(unit_price__isnull=False), name="order_line_priced"),
            models.CheckConstraint(condition=Q(discount__gte=0), name="order_discount_not_negative"),
        ]

    def __str__(self):
        return f"{self.quantity} × {self.sku}"


class OrderEvent(models.Model):
    """Every status change of an order: who, when, from what, to what, why."""

    id = models.BigAutoField(primary_key=True)
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="events")
    at = models.DateTimeField(auto_now_add=True)
    kind = models.CharField(max_length=20, default="status", help_text="status | payment_status | note")
    from_value = models.CharField(max_length=20, blank=True)
    to_value = models.CharField(max_length=20, blank=True)
    actor_type = models.CharField(max_length=16, choices=ActorType.choices)
    actor_label = models.CharField(max_length=255)
    reason = models.CharField(max_length=255, blank=True)
    correlation_id = models.CharField(max_length=128, blank=True)

    class Meta:
        ordering = ["at", "id"]

    def __str__(self):
        return f"{self.order.number}: {self.from_value} → {self.to_value}"
