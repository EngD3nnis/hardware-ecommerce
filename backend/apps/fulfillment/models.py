"""Getting an order's goods to the customer: pick → pack → dispatch/collect → delivered."""

from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel
from apps.sales.models import DeliveryMethod, Order


class FulfillmentStatus(models.TextChoices):
    PICKING = "PICKING", "Picking"
    PACKED = "PACKED", "Packed, ready"
    DISPATCHED = "DISPATCHED", "Out for delivery / collected"
    DELIVERED = "DELIVERED", "Delivered"


class Fulfillment(TimeStampedModel):
    order = models.OneToOneField(Order, on_delete=models.PROTECT, related_name="fulfillment")
    method = models.CharField(max_length=10, choices=DeliveryMethod.choices)
    status = models.CharField(max_length=10, choices=FulfillmentStatus.choices, default=FulfillmentStatus.PICKING)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    driver_name = models.CharField(max_length=100, blank=True)
    vehicle = models.CharField(max_length=50, blank=True)
    recipient_name = models.CharField(max_length=100, blank=True, help_text="Who received the goods.")
    delivery_notes = models.TextField(blank=True)
    failed_attempts = models.PositiveSmallIntegerField(default=0)
    packed_at = models.DateTimeField(null=True, blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    released_unpaid_by = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]
        permissions = [("dispatch_unpaid", "Can release goods before full payment (account customers)")]

    def __str__(self):
        return f"Fulfilment of {self.order.number} ({self.get_status_display()})"
