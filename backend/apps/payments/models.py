"""Payments and refunds.

Webhooks are stored first (InboundPaymentEvent, unique per provider event)
and processed second, so duplicate deliveries are harmless and nothing is
lost if processing fails.
"""

from django.db import models
from django.db.models import Q

from apps.core.models import TimeStampedModel
from apps.sales.models import MONEY, Order


class PaymentMethod(models.TextChoices):
    MPESA = "MPESA", "M-Pesa"
    CASH = "CASH", "Cash"
    BANK = "BANK", "Bank transfer"
    CARD = "CARD", "Card"


class PaymentState(models.TextChoices):
    PENDING = "PENDING", "Pending (waiting for customer/provider)"
    COMPLETED = "COMPLETED", "Completed"
    FAILED = "FAILED", "Failed / cancelled"


class Payment(TimeStampedModel):
    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="payments")
    method = models.CharField(max_length=8, choices=PaymentMethod.choices)
    state = models.CharField(max_length=10, choices=PaymentState.choices, default=PaymentState.PENDING)
    amount = models.DecimalField(**MONEY)
    # Provider's id for the money movement (M-Pesa receipt, bank ref). Unique: one payment per receipt.
    provider_reference = models.CharField(max_length=100, unique=True, null=True, blank=True)
    # For STK push: CheckoutRequestID, used to match the callback to this payment.
    provider_request_id = models.CharField(max_length=100, unique=True, null=True, blank=True)
    payer_phone = models.CharField(max_length=16, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    recorded_by = models.CharField(max_length=255)
    failure_reason = models.CharField(max_length=255, blank=True)
    idempotency_key = models.CharField(max_length=150, unique=True, null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="payment_amount_positive")]

    def __str__(self):
        return f"{self.get_method_display()} {self.amount} for {self.order.number} ({self.state})"


class Refund(TimeStampedModel):
    """Money given back. Recorded by a person after sending it (e.g. M-Pesa reversal, cash)."""

    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="refunds")
    amount = models.DecimalField(**MONEY)
    reason = models.CharField(max_length=255)
    reference = models.CharField(max_length=100, blank=True, help_text="Reversal / transfer reference.")
    recorded_by = models.CharField(max_length=255)

    class Meta:
        permissions = [("record_refund", "Can record refunds")]
        constraints = [models.CheckConstraint(condition=Q(amount__gt=0), name="refund_amount_positive")]

    def __str__(self):
        return f"Refund {self.amount} on {self.payment}"


class EventStatus(models.TextChoices):
    RECEIVED = "RECEIVED", "Received"
    PROCESSED = "PROCESSED", "Processed"
    IGNORED = "IGNORED", "Ignored (duplicate / unknown)"
    FAILED = "FAILED", "Processing failed"


class InboundPaymentEvent(models.Model):
    id = models.BigAutoField(primary_key=True)
    provider = models.CharField(max_length=20)
    event_id = models.CharField(max_length=150)
    payload = models.JSONField()
    received_at = models.DateTimeField(auto_now_add=True)
    source_ip = models.GenericIPAddressField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=EventStatus.choices, default=EventStatus.RECEIVED)
    processed_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-received_at"]
        constraints = [models.UniqueConstraint(fields=["provider", "event_id"], name="payment_event_unique")]

    def __str__(self):
        return f"{self.provider}:{self.event_id} ({self.status})"
