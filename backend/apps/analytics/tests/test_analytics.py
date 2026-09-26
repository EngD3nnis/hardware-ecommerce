from datetime import timedelta
from decimal import Decimal

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.analytics import services
from apps.analytics.models import DailySalesSnapshot
from apps.catalog.tests.factories import ProductFactory
from apps.core.actors import Actor
from apps.core.timeutils import business_today
from apps.customers import services as customers
from apps.fulfillment import services as fulfillment
from apps.inventory import services as inventory
from apps.payments import services as payments
from apps.pricing import services as pricing
from apps.procurement.models import Supplier, SupplierProduct
from apps.sales import services as sales

pytestmark = pytest.mark.django_db


@pytest.fixture
def world(staff_user):
    staff = Actor.for_user(staff_user)
    tap = ProductFactory(sku="TAP-1", name="Tap", price_on_request=False)
    pipe = ProductFactory(sku="PIPE-1", name="Pipe", price_on_request=False)
    idle = ProductFactory(sku="IDLE-1", name="Idle")
    for p, price in ((tap, "1000"), (pipe, "200")):
        pricing.set_price(p, price, staff)
    inventory.receive(tap, 10, staff)
    inventory.receive(pipe, 100, staff)
    inventory.receive(idle, 7, staff)
    supplier = Supplier.objects.create(name="S", code="s")
    SupplierProduct.objects.create(supplier=supplier, product=tap, last_cost="600")
    customer = customers.create_customer(staff, name="C", phone="0700999888")
    order = sales.create_order(
        staff, customer=customer, lines=[{"product": tap, "quantity": 8}, {"product": pipe, "quantity": 5}]
    )
    sales.confirm_order(order, staff)
    sales.allocate_order(order, staff)
    payments.record_payment(order, staff, method="CASH", amount="9000")
    f = fulfillment.start_picking(order, staff)
    fulfillment.mark_packed(f, staff)
    fulfillment.dispatch(f, staff, recipient_name="C")
    quote = sales.create_quotation(staff, customer=customer, lines=[{"product": pipe, "quantity": 1}])
    return {"staff": staff, "tap": tap, "order": order, "quote": quote}


def test_daily_snapshot_is_idempotent(world):
    today = business_today()
    first = services.compute_day(today)
    again = services.compute_day(today)
    assert DailySalesSnapshot.objects.count() == 1
    assert (first.orders, first.orders_delivered, first.revenue_paid) == (1, 1, Decimal("9000.00"))
    assert again.quotations == 1 and again.order_value == Decimal("9000.00")
    assert again.by_channel == {"WALK_IN": 1}


def test_summary_and_top_products(world):
    services.refresh_recent()
    data = services.summary(7)
    assert data["revenue_paid"] == Decimal("9000.00") and data["orders"] == 1
    assert data["average_order_value"] == Decimal("9000.00")
    assert data["quote_conversion_rate"] == 0.0
    assert [p["sku"] for p in data["top_products"]] == ["TAP-1", "PIPE-1"]


def test_inventory_value_uses_known_costs_only(world):
    value = services.inventory_value()
    assert value["value_at_cost"] == Decimal("1200.00")  # 2 taps left × 600
    assert value["lines_without_cost"] == 2  # pipe and idle item: no cost known, not guessed


def test_dead_stock_and_reorder(world):
    assert [d["sku"] for d in services.dead_stock()] == ["IDLE-1"]
    suggestion = {s["sku"]: s for s in services.reorder_suggestions()}
    assert "TAP-1" in suggestion and suggestion["TAP-1"]["available"] == 2
    assert suggestion["TAP-1"]["days_of_cover"] < 28
    assert "PIPE-1" not in suggestion  # 95 left at 5/30 per day


def test_reports_pages_and_api(client, superuser, world):
    services.refresh_recent()
    client.force_login(superuser)
    page = client.get(reverse("analytics-reports") + "?days=7")
    assert page.status_code == 200 and b"TAP-1" in page.content
    csv = client.get(reverse("analytics-daily-csv"))
    assert csv["Content-Type"] == "text/csv" and b"revenue_paid_kes" in csv.content
    api = client.get("/api/v1/analytics/summary/?days=7")
    assert api.status_code == 200 and api.json()["orders"] == 1
    client.logout()
    assert client.get("/api/v1/analytics/summary/").status_code in (401, 403)


def test_old_day_boundaries_use_nairobi_time():
    start, end = services.day_bounds(business_today())
    assert str(start.tzinfo) == "Africa/Nairobi" and end - start == timedelta(days=1)
    assert timezone.now() >= start - timedelta(days=1)
