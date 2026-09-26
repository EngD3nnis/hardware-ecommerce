"""Price lists and effective-dated product prices.

Dewmix quotes most prices individually. "Price on request" is therefore a
normal state, not an error: a product with no current price (or with
Product.price_on_request set) is quoted by a person.

Prices are never edited in place. Setting a new price closes the current
one (valid_to) and adds a new row, so the price on any past date can always
be answered. PostgreSQL rejects overlapping validity periods for the same
product and price list (exclusion constraint).
"""

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeOperators
from django.db import models
from django.db.models import Func, Q

from apps.catalog.models import Product
from apps.core.models import TimeStampedModel


class PriceList(TimeStampedModel):
    code = models.SlugField(max_length=40, unique=True, help_text="e.g. retail, contractor")
    name = models.CharField(max_length=100)
    currency = models.CharField(max_length=3, default="KES")
    is_default = models.BooleanField(default=False)
    prices_include_vat = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-is_default", "name"]
        constraints = [
            models.UniqueConstraint(fields=["is_default"], condition=Q(is_default=True), name="one_default_price_list"),
        ]

    def __str__(self):
        return f"{self.name} ({self.currency})"


class TsTzRange(Func):
    """PostgreSQL tstzrange(lower, upper, bounds); a NULL upper bound means "open ended"."""

    function = "TSTZRANGE"
    output_field = DateTimeRangeField()


class ProductPrice(TimeStampedModel):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="prices")
    price_list = models.ForeignKey(PriceList, on_delete=models.PROTECT, related_name="prices")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    valid_from = models.DateTimeField()
    valid_to = models.DateTimeField(null=True, blank=True, help_text="Empty = current price.")
    source = models.CharField(max_length=60, blank=True, help_text="e.g. admin, import:12")
    created_by = models.CharField(max_length=255, blank=True)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["product", "price_list", "-valid_from"]
        constraints = [
            models.CheckConstraint(condition=Q(amount__gt=0), name="price_amount_positive"),
            models.CheckConstraint(
                condition=Q(valid_to__isnull=True) | Q(valid_to__gt=models.F("valid_from")),
                name="price_valid_to_after_valid_from",
            ),
            ExclusionConstraint(
                name="price_periods_do_not_overlap",
                expressions=[
                    ("product", RangeOperators.EQUAL),
                    ("price_list", RangeOperators.EQUAL),
                    (TsTzRange("valid_from", "valid_to", models.Value("[)")), RangeOperators.OVERLAPS),
                ],
            ),
        ]
        indexes = [models.Index(fields=["product", "price_list", "valid_to"])]

    def __str__(self):
        return f"{self.product.sku} {self.amount} {self.price_list.currency}"
