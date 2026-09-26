import uuid

from django.db import models


class UUIDModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class TimeStampedModel(UUIDModel):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class BusinessProfile(models.Model):
    """Business contact details and customer-facing settings: a single row, edited in admin.

    Everything that used to be hard-coded in the static site (WhatsApp number,
    address, map link, opening hours) lives here so it changes in one place.
    Use `BusinessProfile.get()`.
    """

    SINGLETON_ID = 1

    id = models.PositiveSmallIntegerField(primary_key=True, default=SINGLETON_ID, editable=False)
    business_name = models.CharField(max_length=150, default="Dewmix Hardware")
    legal_name = models.CharField(max_length=200, blank=True)
    kra_pin = models.CharField("KRA PIN", max_length=20, blank=True)
    whatsapp_sales_number = models.CharField(
        max_length=20, help_text="International format without +, e.g. 254787151516. Used for quote links."
    )
    other_phone_numbers = models.JSONField(default=list, blank=True, help_text='JSON list, e.g. ["254743448862"].')
    email = models.EmailField(blank=True)
    website_url = models.URLField(blank=True)
    address = models.TextField(blank=True)
    map_url = models.URLField(blank=True)
    opening_hours = models.JSONField(
        default=list, blank=True, help_text='schema.org style, e.g. ["Mo-Fr 07:00-18:00", "Sa 07:00-17:00"].'
    )
    show_prices_online = models.BooleanField(
        default=False, help_text="Show list prices on the public website/API. Off: every price is on request."
    )
    quote_validity_days = models.PositiveSmallIntegerField(default=14)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "business profile"
        constraints = [models.CheckConstraint(condition=models.Q(id=1), name="business_profile_singleton")]

    def __str__(self):
        return self.business_name

    @classmethod
    def get(cls) -> "BusinessProfile":
        """The profile row. It is created by a data migration, so it always exists."""
        return cls.objects.get(pk=cls.SINGLETON_ID)
