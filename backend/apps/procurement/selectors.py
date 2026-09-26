"""Procurement read queries."""

from decimal import Decimal

from django.db.models import F, Sum

from .models import OPEN_PO_STATUSES, PurchaseOrderLine


def incoming_quantity(product, location=None) -> Decimal:
    """Quantity on approved/sent purchase orders not yet received."""
    lines = PurchaseOrderLine.objects.filter(product=product, order__status__in=OPEN_PO_STATUSES)
    if location is not None:
        lines = lines.filter(order__location=location)
    total = lines.aggregate(total=Sum(F("quantity") - F("received_quantity")))["total"]
    return total or Decimal("0")
