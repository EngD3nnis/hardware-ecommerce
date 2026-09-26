"""Customers. Not login accounts: most Dewmix customers reach the shop on WhatsApp or walk in."""

from django.db import models
from django.db.models import Q

from apps.core.models import TimeStampedModel


class CustomerKind(models.TextChoices):
    INDIVIDUAL = "INDIVIDUAL", "Individual"
    CONTRACTOR = "CONTRACTOR", "Contractor / fundi"
    BUSINESS = "BUSINESS", "Business"


class Customer(TimeStampedModel):
    name = models.CharField(max_length=150)
    kind = models.CharField(max_length=12, choices=CustomerKind.choices, default=CustomerKind.INDIVIDUAL)
    phone = models.CharField(
        max_length=16, null=True, blank=True, unique=True, help_text="E.164 without +, e.g. 254712345678."
    )
    email = models.EmailField(blank=True)
    kra_pin = models.CharField("KRA PIN", max_length=20, blank=True, help_text="For tax invoices.")
    address = models.TextField(blank=True)
    whatsapp_opt_in = models.BooleanField(default=False, help_text="Agreed to receive WhatsApp messages from us.")
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.CheckConstraint(condition=~Q(phone=""), name="customer_phone_null_not_empty")]

    def __str__(self):
        return f"{self.name} ({self.phone})" if self.phone else self.name
