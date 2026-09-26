import pytest
from django.contrib.auth.models import Permission
from django.urls import reverse

from apps.catalog.tests.factories import ProductFactory
from apps.core.actors import Actor
from apps.core.exceptions import Conflict, PermissionDenied, ValidationError
from apps.customers import services as customers
from apps.fulfillment import services
from apps.fulfillment.models import FulfillmentStatus
from apps.inventory import services as inventory
from apps.inventory.models import MovementReason, StockBalance, StockMovement
from apps.payments import services as payments
from apps.pricing import services as pricing
from apps.sales import services as sales
from apps.sales.models import OrderStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff(staff_user):
    return Actor.for_user(staff_user)


_phones = iter(range(700000000, 799999999))


def make_order(staff, method="PICKUP", pay=True):
    product = ProductFactory(price_on_request=False)
    pricing.set_price(product, "500", staff)
    inventory.receive(product, 10, staff)
    customer = customers.create_customer(staff, name="Njeri", phone=f"0{next(_phones)}")
    order = sales.create_order(
        staff,
        customer=customer,
        lines=[{"product": product, "quantity": 3}],
        delivery_method=method,
        delivery_address="Kenol" if method == "DELIVERY" else "",
    )
    sales.confirm_order(order, staff)
    sales.allocate_order(order, staff)
    if pay:
        payments.record_payment(order, staff, method="CASH", amount="1500")
    order.refresh_from_db()
    return order, product


def test_pickup_flow_consumes_reservation_and_completes(staff):
    order, product = make_order(staff)
    f = services.start_picking(order, staff)
    assert services.start_picking(order, staff).pk == f.pk  # idempotent
    services.mark_packed(f, staff)
    with pytest.raises(ValidationError):
        services.dispatch(f, staff, recipient_name="")
    f = services.dispatch(f, staff, recipient_name="Njeri")
    order.refresh_from_db()
    balance = StockBalance.objects.get(product=product)
    assert f.status == FulfillmentStatus.DELIVERED and order.status == OrderStatus.DELIVERED
    assert (balance.on_hand, balance.reserved) == (7, 0)
    sale = StockMovement.objects.get(reason=MovementReason.SALE)
    assert sale.quantity == -3 and sale.source_type == "sales.order" and sale.source_id == str(order.pk)
    assert inventory.reconcile() == []
    assert list(order.events.filter(kind="status").values_list("to_value", flat=True)) == [
        "CONFIRMED",
        "ALLOCATED",
        "PICKING",
        "PACKED",
        "DISPATCHED",
        "DELIVERED",
    ]


def test_delivery_flow_with_failed_attempt(staff):
    order, _ = make_order(staff, method="DELIVERY")
    f = services.start_picking(order, staff)
    services.mark_packed(f, staff)
    f = services.dispatch(f, staff, driver_name="Otieno", vehicle="KDA 123A")
    assert f.status == FulfillmentStatus.DISPATCHED
    services.record_failed_attempt(f, staff, "Customer not home")
    f = services.mark_delivered(f, staff, recipient_name="Site foreman")
    order.refresh_from_db()
    assert order.status == OrderStatus.DELIVERED and f.failed_attempts == 1


def test_unpaid_orders_are_held_unless_permitted(staff, staff_user):
    order, _ = make_order(staff, pay=False)
    f = services.start_picking(order, staff)
    services.mark_packed(f, staff)
    with pytest.raises(Conflict, match="Take payment first"):
        services.dispatch(f, staff, user=staff_user, recipient_name="X")
    staff_user.user_permissions.add(Permission.objects.get(codename="dispatch_unpaid"))
    user = type(staff_user).objects.get(pk=staff_user.pk)
    f = services.dispatch(f, staff, user=user, recipient_name="X")
    assert f.released_unpaid_by == staff.label


def test_cannot_pick_unallocated_or_skip_steps(staff):
    order, _ = make_order(staff)
    f = services.start_picking(order, staff)
    with pytest.raises(Conflict):
        services.dispatch(f, staff, recipient_name="X")  # not packed
    with pytest.raises(PermissionDenied):
        services.mark_packed(f, Actor.agent("ops"))


def test_admin_pages(client, superuser, staff):
    order, _ = make_order(staff)
    services.start_picking(order, staff)
    client.force_login(superuser)
    assert client.get(reverse("admin:fulfillment_fulfillment_changelist")).status_code == 200
    response = client.get(reverse("admin:fulfillment_pick_list", args=[order.pk]))
    assert response.status_code == 200 and order.number.encode() in response.content
