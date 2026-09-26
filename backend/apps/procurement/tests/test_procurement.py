from decimal import Decimal

import pytest
from django.contrib.auth.models import Permission
from django.urls import reverse

from apps.catalog.tests.factories import ProductFactory
from apps.core.actors import Actor
from apps.core.exceptions import Conflict, PermissionDenied, ValidationError
from apps.inventory import services as inventory
from apps.inventory.models import MovementReason, StockMovement
from apps.procurement import services
from apps.procurement.models import POStatus, Supplier, SupplierProduct

pytestmark = pytest.mark.django_db


@pytest.fixture
def supplier():
    return Supplier.objects.create(name="Kamili Pipes Ltd", code="kamili")


@pytest.fixture
def buyer(staff_user):
    return staff_user


@pytest.fixture
def approver(django_user_model):
    user = django_user_model.objects.create_user(email="owner2@dewmix.example", password="x-pass-123456", is_staff=True)
    user.user_permissions.add(Permission.objects.get(codename="approve_purchaseorder"))
    return user


@pytest.fixture
def pipe():
    return ProductFactory(sku="PPR-20")


def make_po(supplier, pipe, who, qty=10, cost="450"):
    return services.create_purchase_order(supplier, [{"product": pipe, "quantity": qty, "unit_cost": cost}], who)


def test_numbering_is_sequential_per_year(supplier, pipe, buyer):
    who = Actor.for_user(buyer)
    a, b = make_po(supplier, pipe, who), make_po(supplier, ProductFactory(), who)
    year = a.created_at.year
    assert (a.number, b.number) == (f"PO-{year}-00001", f"PO-{year}-00002")
    assert a.total == Decimal("4500.00")


def test_full_lifecycle_updates_stock_and_costs(supplier, pipe, buyer, approver):
    order = make_po(supplier, pipe, Actor.for_user(buyer))
    order = services.submit(order, Actor.for_user(buyer), user=buyer)
    assert order.status == POStatus.PENDING_APPROVAL  # default policy: everything needs approval
    order = services.approve(order, Actor.for_user(approver), user=approver)
    order = services.mark_sent(order, Actor.for_user(buyer))
    assert inventory.availability(pipe).incoming == 10

    line = order.lines.get()
    services.receive(order, {line.pk: 4}, Actor.for_user(buyer), idempotency_key="grn-a")
    order.refresh_from_db()
    assert order.status == POStatus.PARTIALLY_RECEIVED
    assert inventory.availability(pipe).on_hand == 4 and inventory.availability(pipe).incoming == 6

    services.receive(order, {line.pk: 4}, Actor.for_user(buyer), idempotency_key="grn-a")  # double submit
    assert inventory.availability(pipe).on_hand == 4

    services.receive(order, {line.pk: "6"}, Actor.for_user(buyer), idempotency_key="grn-b")
    order.refresh_from_db()
    assert order.status == POStatus.RECEIVED and order.received_at
    movement = StockMovement.objects.filter(reason=MovementReason.PURCHASE_RECEIVED).first()
    assert movement.unit_cost == Decimal("450.00") and movement.source_type == "procurement.goodsreceipt"
    assert SupplierProduct.objects.get(supplier=supplier, product=pipe).last_cost == Decimal("450.00")


def test_auto_approve_within_limit(supplier, pipe, approver, settings):
    settings.PURCHASE_AUTO_APPROVE_LIMIT = 5000
    order = services.submit(make_po(supplier, pipe, Actor.for_user(approver)), Actor.for_user(approver), user=approver)
    assert order.status == POStatus.APPROVED and order.approved_by == approver.email


def test_over_limit_still_needs_approval(supplier, pipe, approver, settings):
    settings.PURCHASE_AUTO_APPROVE_LIMIT = 1000
    order = services.submit(make_po(supplier, pipe, Actor.for_user(approver)), Actor.for_user(approver), user=approver)
    assert order.status == POStatus.PENDING_APPROVAL


def test_separation_of_duties(supplier, pipe, approver, settings):
    settings.PURCHASE_APPROVER_MUST_DIFFER = True
    who = Actor.for_user(approver)
    order = services.submit(make_po(supplier, pipe, who), who, user=approver)
    with pytest.raises(PermissionDenied, match="other than the creator"):
        services.approve(order, who, user=approver)


def test_agents_can_draft_but_not_approve_send_or_receive(supplier, pipe, approver):
    agent = Actor.agent("inventory", run_id="r1")
    order = make_po(supplier, pipe, agent)
    assert order.created_by_agent
    order = services.submit(order, agent)
    assert order.status == POStatus.PENDING_APPROVAL
    with pytest.raises(PermissionDenied):
        services.approve(order, agent, user=approver)
    services.approve(order, Actor.for_user(approver), user=approver)
    with pytest.raises(PermissionDenied):
        services.mark_sent(order, agent)
    with pytest.raises(PermissionDenied):
        services.receive(order, {order.lines.get().pk: 1}, agent)


def test_user_without_permission_cannot_approve(supplier, pipe, buyer):
    who = Actor.for_user(buyer)
    order = services.submit(make_po(supplier, pipe, who), who, user=buyer)
    with pytest.raises(PermissionDenied):
        services.approve(order, who, user=buyer)


def test_illegal_transitions_and_over_receipt(supplier, pipe, buyer, approver):
    who = Actor.for_user(buyer)
    order = make_po(supplier, pipe, who)
    with pytest.raises(Conflict):
        services.mark_sent(order, who)  # not approved
    with pytest.raises(Conflict):
        services.receive(order, {order.lines.get().pk: 1}, who)
    services.submit(order, who, user=buyer)
    services.approve(order, Actor.for_user(approver), user=approver)
    with pytest.raises(ValidationError, match="outstanding"):
        services.receive(order, {order.lines.get().pk: 11}, who)


def test_cancel_only_before_receipt(supplier, pipe, buyer, approver):
    who = Actor.for_user(buyer)
    order = make_po(supplier, pipe, who)
    services.submit(order, who, user=buyer)
    services.approve(order, Actor.for_user(approver), user=approver)
    services.receive(order, {order.lines.get().pk: 1}, who)
    with pytest.raises(Conflict):
        services.cancel(order, who, "changed mind")
    other = make_po(supplier, ProductFactory(), who)
    assert services.cancel(other, who, "duplicate").status == POStatus.CANCELLED


def test_line_validation(supplier, pipe, buyer):
    who = Actor.for_user(buyer)
    with pytest.raises(ValidationError, match="No cost known"):
        services.create_purchase_order(supplier, [{"product": pipe, "quantity": 1}], who)
    SupplierProduct.objects.create(supplier=supplier, product=pipe, last_cost="400")
    order = services.create_purchase_order(supplier, [{"product": pipe, "quantity": 2}], who)
    assert order.lines.get().unit_cost == Decimal("400")
    with pytest.raises(ValidationError):
        services.add_line(order, pipe, 1, who, unit_cost="1")  # same product twice
    with pytest.raises(ValidationError):
        services.add_line(order, ProductFactory(), "1.5", who, unit_cost="1")  # pieces are whole
    with pytest.raises(ValidationError):
        services.add_line(order, ProductFactory(), 1, who, unit_cost="-3")


def test_receive_page_in_admin(client, superuser, supplier, pipe, approver):
    who = Actor.for_user(superuser)
    order = make_po(supplier, pipe, who)
    services.submit(order, who, user=superuser)
    services.approve(order, Actor.for_user(approver), user=approver)
    client.force_login(superuser)
    url = reverse("admin:procurement_purchaseorder_receive", args=[order.pk])
    assert client.get(url).status_code == 200
    line = order.lines.get()
    response = client.post(url, {f"line_{line.pk}": "10", "note": "DN 123", "idempotency_key": "abc"})
    assert response.status_code == 302
    order.refresh_from_db()
    assert order.status == POStatus.RECEIVED
    assert inventory.availability(pipe).on_hand == 10
