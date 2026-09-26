"""Deterministic operational checks. Plain queries, no AI, so they work with every agent switched off.

Each check raises (or refreshes) a deduplicated Incident per problem.
"""

from datetime import timedelta

from django.utils import timezone

from apps.inventory import services as inventory
from apps.notifications.models import InboundMessage, MessageStatus, OutboundMessage
from apps.payments.models import EventStatus, InboundPaymentEvent, Payment, PaymentState
from apps.sales.models import Order, OrderStatus

from .models import Severity
from .services import expire_approvals, raise_incident

STUCK_AFTER = {
    OrderStatus.CONFIRMED: ("confirmed_at", timedelta(days=2)),
    OrderStatus.ALLOCATED: ("allocated_at", timedelta(days=3)),
    OrderStatus.PICKING: ("updated_at", timedelta(days=1)),
    OrderStatus.PACKED: ("updated_at", timedelta(days=2)),
    OrderStatus.DISPATCHED: ("dispatched_at", timedelta(days=3)),
}


def stuck_orders() -> int:
    now, count = timezone.now(), 0
    for status, (field, age) in STUCK_AFTER.items():
        for order in Order.objects.filter(status=status, **{f"{field}__lt": now - age}):
            raise_incident(
                kind="stuck_order",
                title=f"Order {order.number} has been {order.get_status_display().lower()} "
                f"for more than {age.days} day(s)",
                fingerprint=f"stuck-order:{order.pk}:{status}",
                source="checks",
                details={"order": order.number, "status": status},
            )
            count += 1
    return count


def failed_messages() -> int:
    failed = OutboundMessage.objects.filter(
        status=MessageStatus.FAILED, updated_at__gte=timezone.now() - timedelta(days=2)
    )
    n = failed.count()
    if n:
        raise_incident(
            kind="failed_messages",
            title=f"{n} customer message(s) failed to send",
            fingerprint="failed-messages",
            source="checks",
            details={"count": n},
        )
    return n


def payment_problems() -> int:
    now = timezone.now()
    failed = InboundPaymentEvent.objects.filter(status=EventStatus.FAILED).count()
    if failed:
        raise_incident(
            kind="payment_events",
            title=f"{failed} payment callback(s) need attention (e.g. amount mismatch)",
            fingerprint="payment-events-failed",
            source="checks",
            severity=Severity.CRITICAL,
            details={"count": failed},
        )
    stale = Payment.objects.filter(state=PaymentState.PENDING, created_at__lt=now - timedelta(hours=1)).count()
    if stale:
        raise_incident(
            kind="stale_mpesa",
            title=f"{stale} M-Pesa request(s) pending for over an hour",
            fingerprint="stale-mpesa",
            source="checks",
            details={"count": stale},
        )
    return failed + stale


def unanswered_whatsapp() -> int:
    n = InboundMessage.objects.filter(handled=False, received_at__lt=timezone.now() - timedelta(minutes=30)).count()
    if n:
        raise_incident(
            kind="unanswered_whatsapp",
            title=f"{n} WhatsApp message(s) unanswered for 30+ minutes",
            fingerprint="unanswered-whatsapp",
            source="checks",
            details={"count": n},
        )
    return n


def inventory_reconciliation() -> int:
    mismatches = inventory.reconcile()
    if mismatches:
        raise_incident(
            kind="inventory_mismatch",
            title=f"Inventory ledger mismatch on {len(mismatches)} balance(s)",
            fingerprint="inventory-mismatch",
            source="checks",
            severity=Severity.CRITICAL,
            details={"mismatches": mismatches[:50]},
        )
    return len(mismatches)


def run_all() -> dict:
    return {
        "stuck_orders": stuck_orders(),
        "failed_messages": failed_messages(),
        "payment_problems": payment_problems(),
        "unanswered_whatsapp": unanswered_whatsapp(),
        "expired_approvals": expire_approvals(),
    }
