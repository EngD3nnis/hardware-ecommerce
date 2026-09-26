"""Business analytics from the transactional tables. Read-only; deterministic."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.db.models import Count, F, Sum
from django.utils import timezone

from apps.core.timeutils import business_today, business_tz
from apps.inventory.models import StockBalance
from apps.payments.models import Payment, PaymentState, Refund
from apps.procurement.models import SupplierProduct
from apps.sales.models import Order, OrderLine, OrderStatus, Quotation

from .models import DailySalesSnapshot

ZERO = Decimal("0")


def day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=business_tz())
    return start, start + timedelta(days=1)


def compute_day(day: date) -> DailySalesSnapshot:
    start, end = day_bounds(day)
    orders = Order.objects.filter(created_at__gte=start, created_at__lt=end)
    quotes = Quotation.objects.filter(created_at__gte=start, created_at__lt=end)
    values = {
        "orders": orders.count(),
        "orders_delivered": Order.objects.filter(delivered_at__gte=start, delivered_at__lt=end).count(),
        "orders_cancelled": Order.objects.filter(cancelled_at__gte=start, cancelled_at__lt=end).count(),
        "revenue_paid": Payment.objects.filter(
            state=PaymentState.COMPLETED, received_at__gte=start, received_at__lt=end
        ).aggregate(t=Sum("amount"))["t"]
        or ZERO,
        "refunds": Refund.objects.filter(created_at__gte=start, created_at__lt=end).aggregate(t=Sum("amount"))["t"]
        or ZERO,
        "order_value": orders.exclude(status=OrderStatus.DRAFT).aggregate(t=Sum("total"))["t"] or ZERO,
        "quotations": quotes.count(),
        "quotations_converted": quotes.filter(order__isnull=False).count(),
        "by_channel": {row["channel"]: row["n"] for row in orders.values("channel").annotate(n=Count("id"))},
    }
    snapshot, _ = DailySalesSnapshot.objects.update_or_create(date=day, defaults=values)
    return snapshot


def refresh_recent(days: int = 3) -> int:
    """Recompute the last few days (late payments/deliveries change recent figures)."""
    today = business_today()
    for offset in range(days):
        compute_day(today - timedelta(days=offset))
    return days


def summary(days: int = 30) -> dict:
    end = business_today()
    start = end - timedelta(days=days - 1)
    snaps = list(DailySalesSnapshot.objects.filter(date__gte=start, date__lte=end).order_by("date"))
    revenue = sum((s.revenue_paid for s in snaps), ZERO)
    orders = sum(s.orders for s in snaps)
    quotes = sum(s.quotations for s in snaps)
    converted = sum(s.quotations_converted for s in snaps)
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "days": days,
        "revenue_paid": revenue,
        "refunds": sum((s.refunds for s in snaps), ZERO),
        "orders": orders,
        "average_order_value": (sum((s.order_value for s in snaps), ZERO) / orders).quantize(Decimal("0.01"))
        if orders
        else ZERO,
        "quotations": quotes,
        "quote_conversion_rate": round(converted / quotes, 3) if quotes else None,
        "daily": [{"date": s.date.isoformat(), "revenue_paid": s.revenue_paid, "orders": s.orders} for s in snaps],
        "top_products": top_products(days),
    }


def top_products(days: int = 30, limit: int = 10) -> list[dict]:
    since = timezone.now() - timedelta(days=days)
    rows = (
        OrderLine.objects.filter(order__status=OrderStatus.DELIVERED, order__delivered_at__gte=since)
        .values("sku", "name")
        .annotate(units=Sum("quantity"), revenue=Sum(F("quantity") * F("unit_price") - F("discount")))
        .order_by("-revenue")[:limit]
    )
    return list(rows)


def inventory_value() -> dict:
    """Stock at last known supplier cost. Items without a known cost are counted separately (not guessed)."""
    costs = {}
    for sp in SupplierProduct.objects.filter(last_cost__isnull=False).order_by("-is_preferred", "-updated_at"):
        costs.setdefault(sp.product_id, sp.last_cost)
    value, unknown = ZERO, 0
    for b in StockBalance.objects.filter(on_hand__gt=0):
        if b.product_id in costs:
            value += b.on_hand * costs[b.product_id]
        else:
            unknown += 1
    return {"value_at_cost": value.quantize(Decimal("0.01")), "lines_without_cost": unknown}


def dead_stock(days: int = 90, limit: int = 50) -> list[dict]:
    since = timezone.now() - timedelta(days=days)
    sold = OrderLine.objects.filter(order__created_at__gte=since).values("product_id")
    rows = (
        StockBalance.objects.filter(on_hand__gt=0)
        .exclude(product_id__in=sold)
        .select_related("product", "location")
        .order_by("-on_hand")[:limit]
    )
    return [
        {"sku": b.product.sku, "name": b.product.name, "location": b.location.code, "on_hand": b.on_hand} for b in rows
    ]


def reorder_suggestions(days: int = 30, cover_days: int = 28, limit: int = 50) -> list[dict]:
    """Items whose available stock covers fewer than `cover_days` at the recent sales rate."""
    since = timezone.now() - timedelta(days=days)
    velocity = {
        row["product_id"]: row["qty"] / days
        for row in OrderLine.objects.filter(order__created_at__gte=since)
        .exclude(order__status=OrderStatus.CANCELLED)
        .values("product_id")
        .annotate(qty=Sum("quantity"))
    }
    suggestions = []
    balances = StockBalance.objects.filter(product_id__in=velocity).select_related("product")
    for b in balances:
        per_day = velocity[b.product_id]
        cover = (b.available / per_day) if per_day else None
        if cover is not None and cover < cover_days:
            need = (per_day * cover_days - b.available).quantize(Decimal("1"))
            suggestions.append(
                {
                    "sku": b.product.sku,
                    "name": b.product.name,
                    "available": b.available,
                    "sold_per_day": per_day.quantize(Decimal("0.01")),
                    "days_of_cover": cover.quantize(Decimal("0.1")),
                    "suggested_quantity": max(need, 1),
                }
            )
    suggestions.sort(key=lambda s: s["days_of_cover"])
    return suggestions[:limit]
