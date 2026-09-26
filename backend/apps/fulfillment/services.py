"""Fulfilment services. Dispatch is where stock actually leaves: reservations are consumed (SALE movements)."""

from django.db import transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.core.actors import Actor
from apps.core.exceptions import Conflict, PermissionDenied, ValidationError
from apps.inventory import services as inventory
from apps.inventory.models import ReservationStatus
from apps.sales import services as sales
from apps.sales.models import DeliveryMethod, Order, OrderStatus, PaymentStatus

from .models import Fulfillment, FulfillmentStatus


def _lock(fulfillment: Fulfillment) -> Fulfillment:
    return Fulfillment.objects.select_for_update().get(pk=fulfillment.pk)


def _require_human(actor: Actor, what: str) -> None:
    if actor.is_agent:
        raise PermissionDenied(f"AI agents cannot {what}.")


@transaction.atomic
def start_picking(order: Order, actor: Actor, *, assigned_to=None) -> Fulfillment:
    """Begin fulfilling an allocated order. Idempotent: returns the existing fulfilment."""
    _require_human(actor, "move goods")
    order = Order.objects.select_for_update().get(pk=order.pk)
    if hasattr(order, "fulfillment"):
        return order.fulfillment
    if order.status != OrderStatus.ALLOCATED:
        raise Conflict(f"Order {order.number} must have stock allocated before picking.")
    fulfillment = Fulfillment.objects.create(order=order, method=order.delivery_method, assigned_to=assigned_to)
    sales.transition(order, OrderStatus.PICKING, actor)
    return fulfillment


@transaction.atomic
def mark_packed(fulfillment: Fulfillment, actor: Actor) -> Fulfillment:
    _require_human(actor, "move goods")
    fulfillment = _lock(fulfillment)
    if fulfillment.status != FulfillmentStatus.PICKING:
        raise Conflict("Only a fulfilment being picked can be marked packed.")
    order = Order.objects.select_for_update().get(pk=fulfillment.order_id)
    sales.transition(order, OrderStatus.PACKED, actor)
    fulfillment.status, fulfillment.packed_at = FulfillmentStatus.PACKED, timezone.now()
    fulfillment.save()
    return fulfillment


def _check_payment(order: Order, actor: Actor, user) -> str:
    if order.payment_status == PaymentStatus.PAID:
        return ""
    if user is not None and user.has_perm("fulfillment.dispatch_unpaid"):
        return actor.label
    raise Conflict(
        f"Order {order.number} is {order.get_payment_status_display().lower()} "
        f"(balance KES {order.balance_due:,.2f}). Take payment first, or ask someone allowed to release unpaid goods.",
        details={"payment_status": order.payment_status, "balance_due": str(order.balance_due)},
    )


@transaction.atomic
def dispatch(
    fulfillment: Fulfillment,
    actor: Actor,
    *,
    user=None,
    driver_name: str = "",
    vehicle: str = "",
    recipient_name: str = "",
) -> Fulfillment:
    """Goods leave the shop (delivery) or are handed to the customer (pickup).

    Consumes the order's reservations, writing SALE stock movements. For pickup
    orders the goods are in the customer's hands, so the order is also marked delivered.
    """
    _require_human(actor, "release goods")
    fulfillment = _lock(fulfillment)
    if fulfillment.status != FulfillmentStatus.PACKED:
        raise Conflict("Pack the order before dispatching it.")
    order = Order.objects.select_for_update().get(pk=fulfillment.order_id)
    fulfillment.released_unpaid_by = _check_payment(order, actor, user)
    if fulfillment.method == DeliveryMethod.PICKUP and not recipient_name.strip():
        raise ValidationError("Enter the name of the person collecting the goods.")

    reservations = list(sales.order_reservations(order).filter(status=ReservationStatus.ACTIVE))
    if not reservations:
        raise Conflict(f"Order {order.number} has no active stock reservations; allocate stock again.")
    for reservation in reservations:
        inventory.consume(reservation, actor, source=inventory.Source.of(order), note=order.number)
    sales.transition(
        order,
        OrderStatus.DISPATCHED,
        actor,
        reason=f"Released unpaid by {actor.label}" if fulfillment.released_unpaid_by else "",
    )
    now = timezone.now()
    fulfillment.status, fulfillment.dispatched_at = FulfillmentStatus.DISPATCHED, now
    fulfillment.driver_name, fulfillment.vehicle, fulfillment.recipient_name = driver_name, vehicle, recipient_name
    if fulfillment.method == DeliveryMethod.PICKUP:
        sales.transition(order, OrderStatus.DELIVERED, actor, reason=f"Collected by {recipient_name}")
        fulfillment.status, fulfillment.delivered_at = FulfillmentStatus.DELIVERED, now
    fulfillment.save()
    audit.record(
        actor,
        "fulfillment.dispatched",
        order,
        after={"method": fulfillment.method, "unpaid_release": bool(fulfillment.released_unpaid_by)},
    )
    return fulfillment


@transaction.atomic
def mark_delivered(fulfillment: Fulfillment, actor: Actor, *, recipient_name: str, notes: str = "") -> Fulfillment:
    if not recipient_name.strip():
        raise ValidationError("Enter who received the goods.")
    fulfillment = _lock(fulfillment)
    if fulfillment.status != FulfillmentStatus.DISPATCHED:
        raise Conflict("Only dispatched goods can be marked delivered.")
    order = Order.objects.select_for_update().get(pk=fulfillment.order_id)
    sales.transition(order, OrderStatus.DELIVERED, actor, reason=f"Received by {recipient_name}")
    fulfillment.status, fulfillment.delivered_at = FulfillmentStatus.DELIVERED, timezone.now()
    fulfillment.recipient_name, fulfillment.delivery_notes = recipient_name, notes
    fulfillment.save()
    return fulfillment


@transaction.atomic
def record_failed_attempt(fulfillment: Fulfillment, actor: Actor, reason: str) -> Fulfillment:
    """Delivery didn't happen (customer absent…). Goods stay out; retry or book a return via stock adjustment."""
    if not reason.strip():
        raise ValidationError("Say why the delivery failed.")
    fulfillment = _lock(fulfillment)
    if fulfillment.status != FulfillmentStatus.DISPATCHED:
        raise Conflict("Only dispatched deliveries can fail.")
    fulfillment.failed_attempts += 1
    fulfillment.delivery_notes = f"{fulfillment.delivery_notes}\nFailed attempt: {reason}".strip()
    fulfillment.save()
    sales.add_note(fulfillment.order, actor, f"Delivery attempt failed: {reason}")
    return fulfillment
