"""Customer messages. The database is the record; WhatsApp/SMS/email are just channels."""

from django.db import models

from apps.core.models import TimeStampedModel
from apps.customers.models import Customer


class ChannelType(models.TextChoices):
    WHATSAPP = "WHATSAPP", "WhatsApp"
    SMS = "SMS", "SMS"
    EMAIL = "EMAIL", "Email"


class MessageStatus(models.TextChoices):
    QUEUED = "QUEUED", "Queued"
    SENT = "SENT", "Sent"
    DELIVERED = "DELIVERED", "Delivered"
    READ = "READ", "Read"
    FAILED = "FAILED", "Failed"
    CANCELLED = "CANCELLED", "Cancelled"


class OutboundMessage(TimeStampedModel):
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, null=True, blank=True, related_name="messages")
    channel = models.CharField(max_length=10, choices=ChannelType.choices)
    to = models.CharField(max_length=254, help_text="Phone (E.164 digits) or email address.")
    template = models.CharField(max_length=50, help_text="What kind of message, e.g. order_confirmed.")
    body = models.TextField()
    status = models.CharField(max_length=10, choices=MessageStatus.choices, default=MessageStatus.QUEUED)
    backend = models.CharField(max_length=20, blank=True, help_text="Adapter that sent it.")
    provider_message_id = models.CharField(max_length=150, unique=True, null=True, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    related_type = models.CharField(max_length=60, blank=True)
    related_id = models.CharField(max_length=64, blank=True)
    created_by = models.CharField(max_length=255, blank=True)
    idempotency_key = models.CharField(max_length=150, unique=True, null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "created_at"]), models.Index(fields=["related_type", "related_id"])]

    def __str__(self):
        return f"{self.get_channel_display()} to {self.to}: {self.template} ({self.status})"


class InboundMessage(models.Model):
    """A message a customer sent us (WhatsApp Cloud API webhook). Stored before anything reads it."""

    id = models.BigAutoField(primary_key=True)
    channel = models.CharField(max_length=10, choices=ChannelType.choices, default=ChannelType.WHATSAPP)
    provider_message_id = models.CharField(max_length=150, unique=True)
    from_phone = models.CharField(max_length=20)
    customer = models.ForeignKey(Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    body = models.TextField(blank=True)
    message_type = models.CharField(max_length=20, default="text")
    payload = models.JSONField()
    received_at = models.DateTimeField(auto_now_add=True)
    handled = models.BooleanField(default=False, help_text="Answered by staff or the sales agent.")
    handled_by = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return f"From {self.from_phone}: {self.body[:40]}"
