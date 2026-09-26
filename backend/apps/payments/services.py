"""Payment recording, M-Pesa STK push, callback processing and refunds."""

import logging
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit import services as audit
from apps.core.actors import Actor
from apps.core.exceptions import Conflict, PermissionDenied, ValidationError
from apps.customers.services import normalise_phone
from apps.sales import services as sales
from apps.sales.models import Order, OrderStatus

from . import mpesa
from .models import EventStatus, InboundPaymentEvent, Payment, PaymentMethod, PaymentState, Refund

logger = logging.getLogger(__name__)


def _amount(raw) -> Decimal:
    try:
        value = Decimal(str(raw).replace(",", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError("Amount must be a number.") from exc
    if not value.is_finite() or value <= 0 or value != value.quantize(Decimal("0.01")):
        raise ValidationError("Amount must be positive with at most 2 decimals.")
    return value


def _payable(order: Order) -> None:
    if order.status in (OrderStatus.DRAFT, OrderStatus.CANCELLED):
        raise Conflict(f"Order {order.number} is {order.get_status_display().lower()}; it cannot take payments.")


@transaction.atomic
def record_payment(
    order: Order,
    actor: Actor,
    *,
    method: str,
    amount,
    reference: str = "",
    idempotency_key: str | None = None,
) -> Payment:
    """A person records money received (cash, bank transfer, M-Pesa paybill paid outside STK)."""
    if actor.is_agent:
        raise PermissionDenied("AI agents cannot record payments.")
    if idempotency_key and (existing := Payment.objects.filter(idempotency_key=idempotency_key).first()):
        return existing
    if method not in PaymentMethod.values:
        raise ValidationError(f"Unknown payment method {method!r}.")
    order = Order.objects.select_for_update().get(pk=order.pk)
    _payable(order)
    value = _amount(amount)
    if method in (PaymentMethod.MPESA, PaymentMethod.BANK) and not reference.strip():
        raise ValidationError("Enter the M-Pesa/bank reference so the payment can be verified.")
    try:
        with transaction.atomic():
            payment = Payment.objects.create(
                order=order,
                method=method,
                state=PaymentState.COMPLETED,
                amount=value,
                provider_reference=reference.strip().upper() or None,
                received_at=timezone.now(),
                recorded_by=actor.label,
                idempotency_key=idempotency_key,
            )
    except IntegrityError as exc:
        raise Conflict(f"Reference {reference} has already been recorded as a payment.") from exc
    audit.record(
        actor,
        "payments.payment.recorded",
        order,
        after={"method": method, "amount": value, "reference": payment.provider_reference},
    )
    sales.refresh_payment_status(order, actor)
    return payment


@transaction.atomic
def request_mpesa_payment(order: Order, actor: Actor, *, phone: str, amount=None) -> Payment:
    """Send an STK push to the customer's phone for the balance due (or `amount`)."""
    order = Order.objects.select_for_update().get(pk=order.pk)
    _payable(order)
    value = _amount(amount if amount is not None else order.balance_due)
    if value != value.to_integral_value():
        raise ValidationError("M-Pesa payments must be whole shillings.")
    msisdn = normalise_phone(phone)
    result = mpesa.stk_push(
        phone=msisdn, amount=int(value), account_reference=order.number, description=f"Dewmix {order.number}"
    )
    payment = Payment.objects.create(
        order=order,
        method=PaymentMethod.MPESA,
        state=PaymentState.PENDING,
        amount=value,
        payer_phone=msisdn,
        provider_request_id=result.checkout_request_id,
        recorded_by=actor.label,
    )
    audit.record(actor, "payments.mpesa.requested", order, after={"amount": value, "phone": msisdn})
    sales.refresh_payment_status(order, actor)
    return payment


def store_mpesa_callback(payload: dict, source_ip: str | None) -> tuple[InboundPaymentEvent, bool]:
    """Persist the raw callback first. Returns (event, created). Duplicates are not stored twice."""
    try:
        event_id = mpesa.parse_stk_callback(payload).checkout_request_id
    except (KeyError, TypeError, ValueError):
        event_id = f"unparseable:{timezone.now().timestamp()}"
    try:
        with transaction.atomic():
            return InboundPaymentEvent.objects.create(
                provider="mpesa", event_id=event_id, payload=payload, source_ip=source_ip
            ), True
    except IntegrityError:
        return InboundPaymentEvent.objects.get(provider="mpesa", event_id=event_id), False


@transaction.atomic
def process_mpesa_event(event: InboundPaymentEvent) -> InboundPaymentEvent:
    """Apply a stored callback to its payment. Idempotent: processed events are skipped."""
    event = InboundPaymentEvent.objects.select_for_update().get(pk=event.pk)
    if event.status != EventStatus.RECEIVED:
        return event
    actor = Actor.integration("mpesa")
    try:
        callback = mpesa.parse_stk_callback(event.payload)
    except (KeyError, TypeError, ValueError) as exc:
        return _finish(event, EventStatus.FAILED, f"Unparseable callback: {exc!r}")
    payment = Payment.objects.select_for_update().filter(provider_request_id=callback.checkout_request_id).first()
    if payment is None:
        return _finish(event, EventStatus.IGNORED, "No payment was requested with this CheckoutRequestID.")
    if payment.state != PaymentState.PENDING:
        return _finish(event, EventStatus.IGNORED, f"Payment already {payment.state}.")

    if not callback.succeeded:
        payment.state, payment.failure_reason = PaymentState.FAILED, callback.result_description[:255]
        payment.save(update_fields=["state", "failure_reason", "updated_at"])
    else:
        paid = Decimal(callback.amount or "0")
        if paid != payment.amount:
            # Never complete a payment for a different amount than we asked for; a person must look.
            logger.error("M-Pesa amount mismatch", extra={"payment": str(payment.pk), "paid": str(paid)})
            return _finish(event, EventStatus.FAILED, f"Amount mismatch: requested {payment.amount}, paid {paid}.")
        if callback.receipt_number and Payment.objects.filter(provider_reference=callback.receipt_number).exists():
            return _finish(event, EventStatus.IGNORED, f"Receipt {callback.receipt_number} already recorded.")
        payment.state = PaymentState.COMPLETED
        payment.provider_reference = callback.receipt_number
        payment.received_at = mpesa.parse_transaction_date(callback.transaction_date) or timezone.now()
        payment.save(update_fields=["state", "provider_reference", "received_at", "updated_at"])
    audit.record(
        actor,
        "payments.mpesa.callback",
        payment.order,
        after={"state": payment.state, "receipt": payment.provider_reference, "amount": payment.amount},
    )
    sales.refresh_payment_status(payment.order, actor)
    return _finish(event, EventStatus.PROCESSED)


def _finish(event: InboundPaymentEvent, status: str, error: str = "") -> InboundPaymentEvent:
    event.status, event.error, event.processed_at = status, error, timezone.now()
    event.save(update_fields=["status", "error", "processed_at"])
    if status == EventStatus.FAILED:
        logger.error("Payment event failed", extra={"event_id": event.event_id, "error": error})
    return event


@transaction.atomic
def record_refund(payment: Payment, actor: Actor, *, amount, reason: str, reference: str = "", user=None) -> Refund:
    """HIGH-risk: a person records money returned to the customer."""
    if actor.is_agent:
        raise PermissionDenied("AI agents cannot issue refunds.")
    if user is not None and not user.has_perm("payments.record_refund"):
        raise PermissionDenied("You do not have permission to record refunds.")
    if not reason.strip():
        raise ValidationError("Give a reason for the refund.")
    payment = Payment.objects.select_for_update().get(pk=payment.pk)
    if payment.state != PaymentState.COMPLETED:
        raise Conflict("Only completed payments can be refunded.")
    value = _amount(amount)
    already = sum((r.amount for r in payment.refunds.all()), Decimal("0"))
    if already + value > payment.amount:
        raise ValidationError(f"Refunds would exceed the payment ({payment.amount}); {already} already refunded.")
    refund = Refund.objects.create(
        payment=payment, amount=value, reason=reason[:255], reference=reference, recorded_by=actor.label
    )
    audit.record(actor, "payments.refund.recorded", payment.order, after={"amount": value}, reason=reason)
    sales.refresh_payment_status(payment.order, actor)
    return refund
