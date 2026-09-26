from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.catalog.tests.factories import ProductFactory
from apps.core.actors import Actor
from apps.core.exceptions import Conflict, PermissionDenied, ValidationError
from apps.customers import services as customers
from apps.inventory import services as inventory
from apps.inventory.models import ReservationStatus, StockBalance
from apps.pricing import services as pricing
from apps.sales import services
from apps.sales.models import OrderEvent, OrderStatus, PaymentStatus, QuoteStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff(staff_user):
    return Actor.for_user(staff_user)


@pytest.fixture
def customer(staff):
    return customers.create_customer(staff, name="Wanjiru Builders", phone="0712 345 678")


@pytest.fixture
def tap(staff):
    product = ProductFactory(sku="TAP-1", name="Basin Tap", price_on_request=False)
    pricing.set_price(product, "1500", staff)
    inventory.receive(product, 10, staff)
    return product


@pytest.fixture
def cement(staff):
    product = ProductFactory(sku="CEM-50", name="Cement 50kg")  # price on request
    inventory.receive(product, 100, staff)
    return product


def test_phone_normalisation(staff):
    assert customers.normalise_phone("+254 712-345-678") == "254712345678"
    assert customers.normalise_phone("0112345678") == "254112345678"
    with pytest.raises(ValidationError):
        customers.normalise_phone("12")
    a, created = customers.get_or_create_by_phone("0712345678", staff, name="A")
    b, created_again = customers.get_or_create_by_phone("+254712345678", staff)
    assert a.pk == b.pk and (created, created_again) == (True, False)


def test_quote_uses_list_prices_and_leaves_on_request_items_unpriced(staff, customer, tap, cement):
    quote = services.create_quotation(
        staff, customer=customer, lines=[{"product": tap, "quantity": 2}, {"product": cement, "quantity": 20}]
    )
    assert quote.number.startswith("Q-")
    lines = {line.sku: line for line in quote.lines.all()}
    assert lines["TAP-1"].unit_price == Decimal("1500.00")
    assert lines["CEM-50"].unit_price is None
    with pytest.raises(ValidationError, match="Price every line"):
        services.send_quotation(quote, staff)
    services.price_quote_line(lines["CEM-50"], staff, unit_price="780", discount="100")
    quote = services.send_quotation(quote, staff)
    assert quote.status == QuoteStatus.SENT and quote.valid_until
    assert quote.total == Decimal("3000.00") + Decimal("15500.00")


def test_agent_quotes_cannot_set_prices(customer, tap, cement):
    agent = Actor.agent("sales", run_id="r1")
    quote = services.create_quotation(agent, customer=customer, lines=[{"product": cement, "quantity": 1}])
    assert quote.created_by_agent and quote.channel == "AGENT"
    with pytest.raises(PermissionDenied):
        services.price_quote_line(quote.lines.get(), agent, unit_price="1")


def test_quote_snapshot_survives_catalogue_changes(staff, customer, tap):
    from apps.catalog import services as catalog

    quote = services.create_quotation(staff, customer=customer, lines=[{"product": tap, "quantity": 1}])
    catalog.update_product(tap, {"name": "Renamed tap"}, staff)
    pricing.set_price(tap, "9999", staff, effective_from=timezone.now() + timedelta(seconds=1))
    line = quote.lines.get()
    assert (line.name, line.unit_price) == ("Basin Tap", Decimal("1500.00"))


def test_convert_quotation_to_order_reserves_stock_and_is_idempotent(staff, customer, tap):
    quote = services.create_quotation(staff, customer=customer, lines=[{"product": tap, "quantity": 3}])
    services.send_quotation(quote, staff)
    services.accept_quotation(quote, staff)
    order = services.convert_to_order(quote, staff)
    again = services.convert_to_order(quote, staff)
    assert order.pk == again.pk
    assert order.status == OrderStatus.ALLOCATED and order.total == Decimal("4500.00")
    assert order.payment_status == PaymentStatus.UNPAID
    assert StockBalance.objects.get(product=tap).reserved == 3
    quote.refresh_from_db()
    assert quote.status == QuoteStatus.CONVERTED
    events = list(order.events.values_list("to_value", flat=True))
    assert events == [OrderStatus.CONFIRMED, OrderStatus.ALLOCATED]


def test_conversion_without_stock_keeps_order_confirmed(staff, customer):
    scarce = ProductFactory(price_on_request=False)
    pricing.set_price(scarce, "100", staff)
    inventory.receive(scarce, 1, staff)
    quote = services.create_quotation(staff, customer=customer, lines=[{"product": scarce, "quantity": 5}])
    services.send_quotation(quote, staff)
    services.accept_quotation(quote, staff)
    order = services.convert_to_order(quote, staff)
    assert order.status == OrderStatus.CONFIRMED
    assert OrderEvent.objects.filter(order=order, kind="note", reason__contains="Could not allocate").exists()


def test_expired_quotation_cannot_be_accepted(staff, customer, tap):
    quote = services.create_quotation(staff, customer=customer, lines=[{"product": tap, "quantity": 1}])
    services.send_quotation(quote, staff)
    type(quote).objects.filter(pk=quote.pk).update(valid_until=timezone.localdate() - timedelta(days=2))
    assert services.expire_quotations() == 1
    with pytest.raises(Conflict):
        services.accept_quotation(quote, staff)


def test_illegal_order_transitions_are_refused(staff, customer, tap):
    order = services.create_order(staff, customer=customer, lines=[{"product": tap, "quantity": 1}])
    with pytest.raises(Conflict):
        services.transition(order, OrderStatus.DELIVERED, staff)
    services.confirm_order(order, staff)
    services.allocate_order(order, staff)
    order.refresh_from_db()
    for status in (OrderStatus.DISPATCHED, OrderStatus.DELIVERED):
        services.transition(order, status, staff)
    for status in (OrderStatus.PICKING, OrderStatus.ALLOCATED, OrderStatus.CANCELLED):
        with pytest.raises(Conflict):  # a delivered order never goes back to picking
            services.transition(order, status, staff)


def test_cancel_releases_stock_and_needs_reason_and_human(staff, customer, tap):
    order = services.create_order(staff, customer=customer, lines=[{"product": tap, "quantity": 4}])
    services.confirm_order(order, staff)
    services.allocate_order(order, staff)
    with pytest.raises(ValidationError):
        services.cancel_order(order, staff, "")
    with pytest.raises(PermissionDenied):
        services.cancel_order(order, Actor.agent("ops"), "agent wants to")
    services.cancel_order(order, staff, "Customer changed mind")
    assert StockBalance.objects.get(product=tap).reserved == 0
    assert all(r.status == ReservationStatus.RELEASED for r in services.order_reservations(order))


def test_direct_order_needs_prices(staff, customer, cement):
    with pytest.raises(ValidationError, match="no list price"):
        services.create_order(staff, customer=customer, lines=[{"product": cement, "quantity": 1}])
    order = services.create_order(
        staff, customer=customer, lines=[{"product": cement, "quantity": 2, "unit_price": "800"}]
    )
    assert order.lines.get().unit_price == Decimal("800.00")
    again = services.create_order(
        staff, customer=customer, lines=[{"product": cement, "quantity": 2, "unit_price": "800"}], idempotency_key="k1"
    )
    assert services.create_order(staff, customer=customer, lines=[], idempotency_key="k1").pk == again.pk
