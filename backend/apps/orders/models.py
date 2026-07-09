from django.db import models
from django.conf import settings
from apps.common.models import TimeStampedModel
from apps.catalog.models import Product

class Order(TimeStampedModel):
    STATUS_CHOICES = (
        ('Pending', 'Pending'),
        ('Paid', 'Paid'),
        ('Shipped', 'Shipped'),
        ('Cancelled', 'Cancelled'),
        ('Returned', 'Returned'),
    )

    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='orders')
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Pending')
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    shipping_address = models.TextField()
    phone_number = models.CharField(max_length=50)
    whatsapp_notified = models.BooleanField(default=False)

    def __str__(self):
        return f"Order {self.id} - Status: {self.status}"


class OrderItem(TimeStampedModel):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='order_items')
    quantity = models.IntegerField()
    price = models.DecimalField(max_digits=12, decimal_places=2)

    def __str__(self):
        return f"{self.quantity} x {self.product.name} in Order {self.order.id}"


class Invoice(TimeStampedModel):
    order = models.OneToOneField(Order, on_delete=models.PROTECT, related_name='invoice')
    invoice_number = models.CharField(max_length=100, unique=True)
    pdf_url = models.URLField(max_length=500, blank=True)

    def __str__(self):
        return self.invoice_number
