from django.db import models
from apps.common.models import TimeStampedModel
from apps.orders.models import Order

class PaymentTransaction(TimeStampedModel):
    METHOD_CHOICES = (
        ('M-Pesa', 'M-Pesa'),
        ('Card', 'Card'),
        ('Bank Transfer', 'Bank Transfer'),
    )

    STATUS_CHOICES = (
        ('Pending', 'Pending'),
        ('Completed', 'Completed'),
        ('Failed', 'Failed'),
        ('Refunded', 'Refunded'),
    )

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name='payments')
    method = models.CharField(max_length=50, choices=METHOD_CHOICES)
    reference_id = models.CharField(max_length=255, unique=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Pending')
    raw_response = models.JSONField(null=True, blank=True)

    def __str__(self):
        return f"{self.method} Ref: {self.reference_id} - Status: {self.status}"
