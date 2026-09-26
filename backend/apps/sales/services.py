"""Quotations and orders: the only code that changes them."""

from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.catalog.models import Product, ProductStatus
from apps.core.actors import Actor
from apps.core.exceptions import Conflict, PermissionDenied, ValidationError
from apps.core.models import BusinessProfile
from apps.core.numbering import next_number
from apps.core.request_context import get_correlation_id
from apps.core.timeutils import business_today
from apps.customers.models import Customer
from apps.inventory import services as inventory
from apps.inventory.models import Reservation, ReservationStatus
from apps.notifications import services as notifications
from apps.pricing import services as pricing

from .models import (
    Channel,
    Order,
    OrderEvent,
    OrderLine,
    OrderStatus,
    PaymentStatus,
    Quotation,
    QuotationLine,
    QuoteStatus,
)

MONEY_STEP = Decimal("0.01")

# Legal order status moves. Anything else is refused.
ORDER_TRANSITIONS = {
    OrderStatus.DRAFT: {OrderStatus.CONFIRMED, OrderStatus.CANCELLED},
    OrderStatus.CONFIRMED: {OrderStatus.ALLOCATED, OrderStatus.CANCELLED},
    OrderStatus.ALLOCATED: {OrderStatus.PICKING, OrderStatus.DISPATCHED, OrderStatus.CANCELLED},
    OrderStatus.PICKING: {OrderStatus.PACKED, OrderStatus.CANCELLED},
    OrderStatus.PACKED: {OrderStatus.DISPATCHED, OrderStatus.CANCELLED},
    OrderStatus.DISPATCHED: {OrderStatus.DELIVERED},
    OrderStatus.DELIVERED: set(),  # returns/refunds are separate workflows, not a status rewind
    OrderStatus.CANCELLED: set(),
}
TIMESTAMP_FOR = {
    OrderStatus.CONFIRMED: "confirmed_at",
    OrderStatus.ALLOCATED: "allocated_at",
    OrderStatus.DISPATCHED: "dispatched_at",
    OrderStatus.DELIVERED: "delivered_at",
    OrderStatus.CANCELLED: "cancelled_at",
}


def _snapshot(product: Product) -> dict:
    attributes = {v.attribute.code: str(v.value) for v in product.attribute_values.select_related("attribute")}
    return {
        "product": product,
        "sku": product.sku,
        "name": product.name,
        "unit_of_measure": product.unit_of_measure,
        "attributes": attributes,
    }


def _check_sellable(product: Product) -> None:
    if product.status != ProductStatus.ACTIVE:
        raise ValidationError(f"{product.sku} is not available for sale.", details={"sku": product.sku})


def _money(raw, field="price") -> Decimal:
    try:
        value = Decimal(str(raw).replace(",", "").strip())
    except Exception as exc:  # noqa: BLE001 - any parse failure is a validation error
        raise ValidationError(f"{field} must be a number.") from exc
    if not value.is_finite() or value < 0 or value != value.quantize(MONEY_STEP):
        raise ValidationError(f"{field} must be zero or more with at most 2 decimals.")
    return value


# --- Quotations -------------------------------------------------------------------------------


def _lock_quote(quote: Quotation) -> Quotation:
    return Quotation.objects.select_for_update().get(pk=quote.pk)


@transaction.atomic
def create_quotation(
    actor: Actor,
    *,
    customer: Customer | None,
    lines: list[dict],
    channel: str = Channel.WALK_IN,
    notes: str = "",
    idempotency_key: str | None = None,
) -> Quotation:
    """lines: [{"product": Product, "quantity": n}]. Prices come from the price list, never from the caller.

    Lines without a list price are left unpriced ("price on request") for a person to price.
    """
    if idempotency_key and (existing := Quotation.objects.filter(idempotency_key=idempotency_key).first()):
        return existing
    if not lines:
        raise ValidationError("A quotation needs at least one product.")
    price_list = pricing.default_price_list()
    quote = Quotation.objects.create(
        number=next_number("Q"),
        customer=customer,
        channel=Channel.AGENT if actor.is_agent else channel,
        notes=notes,
        prices_include_vat=price_list.prices_include_vat,
        created_by=actor.label,
        created_by_agent=actor.is_agent,
        idempotency_key=idempotency_key,
    )
    for line in lines:
        _add_quote_line(quote, line["product"], line["quantity"])
    audit.record(actor, "sales.quotation.created", quote, after={"lines": len(lines), "channel": quote.channel})
    return quote


def _add_quote_line(quote: Quotation, product: Product, quantity) -> QuotationLine:
    _check_sellable(product)
    qty = inventory.validate_quantity(product, quantity)
    if quote.lines.filter(product=product).exists():
        raise ValidationError(f"{product.sku} is already on this quotation.")
    price = pricing.quote_price(product)
    return QuotationLine.objects.create(quotation=quote, quantity=qty, unit_price=price.amount, **_snapshot(product))


@transaction.atomic
def add_quote_line(quote: Quotation, product: Product, quantity, actor: Actor) -> QuotationLine:
    quote = _lock_quote(quote)
    if quote.status != QuoteStatus.DRAFT:
        raise Conflict("Only draft quotations can be changed.")
    line = _add_quote_line(quote, product, quantity)
    audit.record(actor, "sales.quotation.line_added", quote, after={"sku": product.sku, "quantity": line.quantity})
    return line


@transaction.atomic
def price_quote_line(line: QuotationLine, actor: Actor, *, unit_price, discount="0") -> QuotationLine:
    """A person sets or overrides a line price (e.g. for price-on-request items). Agents may not."""
    if actor.is_agent:
        raise PermissionDenied("AI agents cannot set prices.")
    quote = _lock_quote(line.quotation)
    if quote.status != QuoteStatus.DRAFT:
        raise Conflict("Only draft quotations can be re-priced.")
    line = QuotationLine.objects.get(pk=line.pk)
    before = {"unit_price": line.unit_price, "discount": line.discount}
    line.unit_price, line.discount = _money(unit_price, "Unit price"), _money(discount, "Discount")
    if line.discount > line.quantity * line.unit_price:
        raise ValidationError("Discount cannot exceed the line value.")
    line.save(update_fields=["unit_price", "discount"])
    audit.record(
        actor,
        "sales.quotation.priced",
        quote,
        before={line.sku: before},
        after={line.sku: {"unit_price": line.unit_price, "discount": line.discount}},
    )
    return line


@transaction.atomic
def remove_quote_line(line: QuotationLine, actor: Actor) -> None:
    quote = _lock_quote(line.quotation)
    if quote.status != QuoteStatus.DRAFT:
        raise Conflict("Only draft quotations can be changed.")
    sku = line.sku
    line.delete()
    audit.record(actor, "sales.quotation.line_removed", quote, before={"sku": sku})


def _set_quote_status(quote: Quotation, to: str, actor: Actor, reason: str = "") -> None:
    before = quote.status
    quote.status = to
    audit.record(actor, "sales.quotation.status", quote, before={"status": before}, after={"status": to}, reason=reason)


@transaction.atomic
def send_quotation(quote: Quotation, actor: Actor) -> Quotation:
    """Mark as sent to the customer. Every line must be priced; the validity period starts now."""
    quote = _lock_quote(quote)
    if quote.status != QuoteStatus.DRAFT:
        raise Conflict("Only a draft quotation can be sent.")
    if not quote.lines.exists():
        raise ValidationError("The quotation has no lines.")
    if not quote.is_fully_priced:
        raise ValidationError(
            "Price every line before sending.",
            details={"unpriced": [line.sku for line in quote.lines.filter(unit_price__isnull=True)]},
        )
    quote.valid_until = business_today() + timedelta(days=BusinessProfile.get().quote_validity_days)
    quote.sent_at = timezone.now()
    _set_quote_status(quote, QuoteStatus.SENT, actor)
    quote.save()
    if quote.customer:
        notifications.notify("quote_sent", quote, customer=quote.customer, actor=actor)
    return quote


@transaction.atomic
def accept_quotation(quote: Quotation, actor: Actor) -> Quotation:
    quote = _lock_quote(quote)
    if quote.status != QuoteStatus.SENT:
        raise Conflict("Only a sent quotation can be accepted.")
    if quote.valid_until and quote.valid_until < business_today():
        _set_quote_status(quote, QuoteStatus.EXPIRED, actor, reason="Accepted after expiry")
        quote.save()
        raise Conflict("This quotation has expired; issue a new one (prices may have changed).")
    quote.decided_at = timezone.now()
    _set_quote_status(quote, QuoteStatus.ACCEPTED, actor)
    quote.save()
    return quote


@transaction.atomic
def reject_quotation(quote: Quotation, actor: Actor, reason: str = "") -> Quotation:
    quote = _lock_quote(quote)
    if quote.status not in (QuoteStatus.DRAFT, QuoteStatus.SENT, QuoteStatus.ACCEPTED):
        raise Conflict("This quotation can no longer be rejected.")
    quote.decided_at = timezone.now()
    _set_quote_status(quote, QuoteStatus.REJECTED, actor, reason=reason)
    quote.save()
    return quote


def expire_quotations(actor: Actor | None = None) -> int:
    """Mark sent quotations past their validity date as EXPIRED (beat task)."""
    actor = actor or Actor.system("quotation_expiry")
    expired = 0
    for quote in Quotation.objects.filter(status=QuoteStatus.SENT, valid_until__lt=business_today()):
        with transaction.atomic():
            locked = _lock_quote(quote)
            if locked.status == QuoteStatus.SENT:
                _set_quote_status(locked, QuoteStatus.EXPIRED, actor)
                locked.save()
                expired += 1
    return expired


@transaction.atomic
def convert_to_order(
    quote: Quotation,
    actor: Actor,
    *,
    delivery_method: str = "PICKUP",
    delivery_address: str = "",
    allocate: bool = True,
) -> Order:
    """Turn an accepted quotation into a confirmed order, copying its lines (and prices) exactly.

    Idempotent: converting the same quotation again returns the existing order.
    With allocate=True, stock is reserved now if available; if not, the order
    stays CONFIRMED (the shortage is reported, the order is not lost).
    """
    quote = _lock_quote(quote)
    if hasattr(quote, "order"):
        return quote.order
    if quote.status != QuoteStatus.ACCEPTED:
        raise Conflict("Only an accepted quotation can become an order.")
    if quote.customer is None:
        raise ValidationError("Add the customer to the quotation first.")
    order = Order.objects.create(
        number=next_number("SO"),
        customer=quote.customer,
        quotation=quote,
        channel=quote.channel,
        delivery_method=delivery_method,
        delivery_address=delivery_address,
        prices_include_vat=quote.prices_include_vat,
        created_by=actor.label,
    )
    for line in quote.lines.all():
        OrderLine.objects.create(
            order=order,
            product=line.product,
            sku=line.sku,
            name=line.name,
            unit_of_measure=line.unit_of_measure,
            attributes=line.attributes,
            quantity=line.quantity,
            unit_price=line.unit_price,
            discount=line.discount,
        )
    _set_quote_status(quote, QuoteStatus.CONVERTED, actor, reason=f"Order {order.number}")
    quote.save()
    confirm_order(order, actor)
    if allocate:
        try:
            with transaction.atomic():
                allocate_order(order, actor)
        except inventory.InsufficientStock as exc:
            add_note(order, actor, f"Could not allocate stock yet: {exc.message}")
    order.refresh_from_db()
    return order


# --- Orders ---------------------------------------------------------------------------------------


def _lock_order(order: Order) -> Order:
    return Order.objects.select_for_update().get(pk=order.pk)


def _event(order: Order, actor: Actor, kind: str, from_value: str, to_value: str, reason: str = "") -> None:
    OrderEvent.objects.create(
        order=order,
        kind=kind,
        from_value=from_value,
        to_value=to_value,
        actor_type=actor.type,
        actor_label=actor.label[:255],
        reason=reason[:255],
        correlation_id=get_correlation_id() or "",
    )


def transition(order: Order, to: str, actor: Actor, reason: str = "") -> Order:
    """Move an order to `to` if the move is legal. Callers hold the order lock."""
    if to not in ORDER_TRANSITIONS[OrderStatus(order.status)]:
        raise Conflict(
            f"Order {order.number} is {order.get_status_display().lower()} and cannot become "
            f"{OrderStatus(to).label.lower()}.",
            details={"from": order.status, "to": to},
        )
    before = order.status
    order.status = to
    if to in TIMESTAMP_FOR:
        setattr(order, TIMESTAMP_FOR[to], timezone.now())
    order.save()
    _event(order, actor, "status", before, to, reason)
    audit.record(actor, "sales.order.status", order, before={"status": before}, after={"status": to}, reason=reason)
    return order


@transaction.atomic
def create_order(
    actor: Actor,
    *,
    customer: Customer,
    lines: list[dict],
    channel: str = Channel.WALK_IN,
    delivery_method: str = "PICKUP",
    delivery_address: str = "",
    idempotency_key: str | None = None,
) -> Order:
    """Counter sale / direct order without a quotation. Every line must have a price
    (list price, or an explicit unit_price from a person)."""
    if idempotency_key and (existing := Order.objects.filter(idempotency_key=idempotency_key).first()):
        return existing
    if not lines:
        raise ValidationError("An order needs at least one product.")
    order = Order.objects.create(
        number=next_number("SO"),
        customer=customer,
        channel=channel,
        delivery_method=delivery_method,
        delivery_address=delivery_address,
        created_by=actor.label,
        idempotency_key=idempotency_key,
        prices_include_vat=pricing.default_price_list().prices_include_vat,
    )
    for line in lines:
        product = line["product"]
        _check_sellable(product)
        qty = inventory.validate_quantity(product, line["quantity"])
        if line.get("unit_price") is not None:
            if actor.is_agent:
                raise PermissionDenied("AI agents cannot set prices.")
            unit_price = _money(line["unit_price"], "Unit price")
        else:
            unit_price = pricing.quote_price(product).amount
            if unit_price is None:
                raise ValidationError(f"{product.sku} has no list price; enter a price.", details={"sku": product.sku})
        OrderLine.objects.create(order=order, quantity=qty, unit_price=unit_price, **_snapshot(product))
    audit.record(actor, "sales.order.created", order, after={"lines": len(lines)})
    return order


@transaction.atomic
def confirm_order(order: Order, actor: Actor) -> Order:
    order = _lock_order(order)
    if not order.lines.exists():
        raise ValidationError("The order has no lines.")
    order.total = sum((line.line_total for line in order.lines.all()), Decimal("0.00"))
    transition(order, OrderStatus.CONFIRMED, actor)
    notifications.notify("order_confirmed", order, customer=order.customer, actor=actor)
    return order


@transaction.atomic
def allocate_order(order: Order, actor: Actor) -> Order:
    """Reserve stock for every line, all or nothing."""
    order = _lock_order(order)
    if order.status == OrderStatus.ALLOCATED:
        return order
    for line in order.lines.select_related("product").order_by("product_id"):
        inventory.reserve(
            line.product,
            line.quantity,
            actor,
            source=inventory.Source.of(order),
            idempotency_key=f"order:{order.pk}:line:{line.pk}",
        )
    return transition(order, OrderStatus.ALLOCATED, actor)


def order_reservations(order: Order):
    return Reservation.objects.filter(source_type="sales.order", source_id=str(order.pk))


@transaction.atomic
def cancel_order(order: Order, actor: Actor, reason: str) -> Order:
    """Cancel before dispatch. Reserved stock is released.

    A cancelled order must not keep customer money silently: if anything was
    paid and not refunded, cancelling is refused until a refund is recorded.
    """
    if not (reason or "").strip():
        raise ValidationError("Give a reason for cancelling.")
    if actor.is_agent:
        raise PermissionDenied("AI agents cannot cancel orders.")
    order = _lock_order(order)
    if order.amount_paid - order.amount_refunded > 0:
        raise Conflict(
            f"{order.number} has KES {order.amount_paid - order.amount_refunded:,.2f} paid. Record the refund first.",
            details={"paid": str(order.amount_paid), "refunded": str(order.amount_refunded)},
        )
    transition(order, OrderStatus.CANCELLED, actor, reason=reason)
    order.cancel_reason = reason[:255]
    order.save(update_fields=["cancel_reason", "updated_at"])
    for reservation in order_reservations(order).filter(status=ReservationStatus.ACTIVE):
        inventory.release(reservation, actor, reason=f"Order {order.number} cancelled")
    return order


@transaction.atomic
def add_note(order: Order, actor: Actor, text: str) -> None:
    _event(order, actor, "note", "", "", text)


@transaction.atomic
def refresh_payment_status(order: Order, actor: Actor) -> Order:
    """Recompute amount_paid / amount_refunded / payment_status from the payment records."""
    from apps.payments.models import Payment, PaymentState, Refund

    order = _lock_order(order)
    paid = sum((p.amount for p in Payment.objects.filter(order=order, state=PaymentState.COMPLETED)), Decimal("0"))
    refunded = sum((r.amount for r in Refund.objects.filter(payment__order=order)), Decimal("0"))
    pending = Payment.objects.filter(order=order, state=PaymentState.PENDING).exists()
    net = paid - refunded
    if refunded and net <= 0:
        status = PaymentStatus.REFUNDED
    elif refunded:
        status = PaymentStatus.PARTIALLY_REFUNDED
    elif paid >= order.total > 0:
        status = PaymentStatus.PAID
    elif paid > 0:
        status = PaymentStatus.PARTIALLY_PAID
    elif pending:
        status = PaymentStatus.PENDING
    else:
        status = PaymentStatus.UNPAID
    before = order.payment_status
    order.amount_paid, order.amount_refunded, order.payment_status = paid, refunded, status
    order.save(update_fields=["amount_paid", "amount_refunded", "payment_status", "updated_at"])
    if before != status:
        _event(order, actor, "payment_status", before, status)
    return order
