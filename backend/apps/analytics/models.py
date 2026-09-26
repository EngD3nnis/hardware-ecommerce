from django.db import models


class DailySalesSnapshot(models.Model):
    """Figures for one business day (Nairobi time), recomputed idempotently by a nightly task."""

    date = models.DateField(primary_key=True)
    orders = models.PositiveIntegerField(default=0)
    orders_delivered = models.PositiveIntegerField(default=0)
    orders_cancelled = models.PositiveIntegerField(default=0)
    revenue_paid = models.DecimalField(max_digits=14, decimal_places=2, default=0, help_text="Payments received.")
    refunds = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    order_value = models.DecimalField(max_digits=14, decimal_places=2, default=0, help_text="Total of orders created.")
    quotations = models.PositiveIntegerField(default=0)
    quotations_converted = models.PositiveIntegerField(default=0)
    by_channel = models.JSONField(default=dict)
    computed_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date"]

    def __str__(self):
        return f"{self.date}: {self.orders} orders, KES {self.revenue_paid}"
