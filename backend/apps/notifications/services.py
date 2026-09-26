"""Queue, send and receive customer messages.

- queue(): writes an OutboundMessage (idempotent per key) and sends it via
  Celery after the surrounding transaction commits.
- notify(): the call business services make ("order confirmed" etc.). It never
  lets a messaging problem break the business transaction: a failure is logged
  and the order carries on.
- send(): the task body. Transient errors retry with backoff; permanent ones
  mark the message FAILED for a person (or the Operations agent) to see.
"""

import hashlib
import hmac
import logging
from urllib.parse import quote as urlquote

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.core.actors import Actor
from apps.core.models import BusinessProfile
from apps.customers.models import Customer
from apps.customers.services import normalise_phone

from . import adapters
from .models import ChannelType, InboundMessage, MessageStatus, OutboundMessage

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 5


# --- Message texts (plain, short, no promises the system can't keep) ---------------------------


def _money(amount) -> str:
    return f"KES {amount:,.2f}"


def render(template: str, obj) -> str:
    name = BusinessProfile.get().business_name
    if template == "quote_sent":
        lines = "\n".join(
            f"- {line.quantity.normalize():f} x {line.name}: {_money(line.line_total)}" for line in obj.lines.all()
        )
        return (
            f"{name}: quotation {obj.number}\n{lines}\nTotal: {_money(obj.total)}"
            f"{' (VAT inclusive)' if obj.prices_include_vat else ''}\nValid until {obj.valid_until:%d %b %Y}. "
            f"Reply to accept or ask questions."
        )
    if template == "order_confirmed":
        return (
            f"{name}: order {obj.number} is confirmed. Total {_money(obj.total)}. We'll let you know when it's ready."
        )
    if template == "payment_received":
        return f"{name}: we received {_money(obj.amount)} for order {obj.order.number}. Thank you!"
    if template == "ready_for_pickup":
        return f"{name}: order {obj.number} is packed and ready for collection."
    if template == "order_dispatched":
        return f"{name}: order {obj.number} is on its way."
    raise ValueError(f"Unknown message template {template!r}")


# --- Outbound -------------------------------------------------------------------------------------


def _recipient(customer: Customer, channel: str, customer_initiated: bool = False) -> str | None:
    if channel == ChannelType.EMAIL:
        return customer.email or None
    if channel == ChannelType.WHATSAPP and not (customer.whatsapp_opt_in or customer_initiated):
        return None  # business-initiated WhatsApp needs the customer's consent
    return customer.phone


def queue(
    *,
    customer: Customer,
    channel: str,
    template: str,
    body: str,
    related=None,
    actor: Actor,
    idempotency_key: str | None = None,
    customer_initiated: bool = False,
) -> OutboundMessage | None:
    """Store a message and send it after commit. Returns None if the customer can't receive on this channel.

    customer_initiated: replying to a message the customer just sent (allowed without opt-in).
    """
    to = _recipient(customer, channel, customer_initiated)
    if not to:
        return None
    if idempotency_key and (existing := OutboundMessage.objects.filter(idempotency_key=idempotency_key).first()):
        return existing
    try:
        with transaction.atomic():
            message = OutboundMessage.objects.create(
                customer=customer,
                channel=channel,
                to=to,
                template=template,
                body=body,
                related_type=related._meta.label_lower if related is not None else "",
                related_id=str(related.pk) if related is not None else "",
                created_by=actor.label,
                idempotency_key=idempotency_key,
            )
    except IntegrityError:
        return OutboundMessage.objects.get(idempotency_key=idempotency_key)
    from .tasks import send_message

    message_id = str(message.pk)
    transaction.on_commit(lambda: send_message.delay(message_id))
    return message


def notify(template: str, obj, *, customer: Customer, actor: Actor) -> OutboundMessage | None:
    """Business event → customer message on their preferred channel. Never raises."""
    try:
        with transaction.atomic():
            return queue(
                customer=customer,
                channel=ChannelType.WHATSAPP,
                template=template,
                body=render(template, obj),
                related=obj,
                actor=actor,
                idempotency_key=f"{template}:{obj._meta.label_lower}:{obj.pk}",
            )
    except Exception:  # noqa: BLE001 - messaging must never break a sale; logged for follow-up
        logger.exception("Could not queue customer notification", extra={"template": template})
        return None


def send(message_id: str) -> OutboundMessage:
    """Send one queued message. Raises TransientSendError for the task to retry."""
    with transaction.atomic():
        message = OutboundMessage.objects.select_for_update().get(pk=message_id)
        if message.status != MessageStatus.QUEUED:
            return message  # already sent/cancelled: idempotent
        message.attempts += 1
        message.save(update_fields=["attempts", "updated_at"])
    adapter = adapters.adapter_for(message.channel)
    try:
        result = adapter.send(message)
    except adapters.TransientSendError as exc:
        OutboundMessage.objects.filter(pk=message.pk).update(last_error=str(exc)[:1000])
        if message.attempts >= MAX_ATTEMPTS:
            return _fail(message, f"Gave up after {message.attempts} attempts: {exc}")
        raise
    except adapters.PermanentSendError as exc:
        return _fail(message, str(exc))
    message.status, message.backend, message.sent_at = MessageStatus.SENT, adapter.name, timezone.now()
    message.provider_message_id, message.last_error = result.provider_message_id, ""
    message.save(update_fields=["status", "backend", "sent_at", "provider_message_id", "last_error", "updated_at"])
    return message


def _fail(message: OutboundMessage, error: str) -> OutboundMessage:
    message.status, message.last_error = MessageStatus.FAILED, error[:1000]
    message.save(update_fields=["status", "last_error", "updated_at"])
    logger.error("Customer message failed", extra={"message_id": str(message.pk), "error": error})
    return message


def retry(message: OutboundMessage) -> None:
    """Human override: requeue a failed message."""
    if message.status == MessageStatus.FAILED:
        OutboundMessage.objects.filter(pk=message.pk).update(status=MessageStatus.QUEUED, last_error="")
        from .tasks import send_message

        send_message.delay(str(message.pk))


def whatsapp_click_to_chat(phone: str, text: str) -> str:
    """wa.me link a staff member can open to send `text` from their own WhatsApp (no API needed)."""
    return f"https://wa.me/{normalise_phone(phone)}?text={urlquote(text)}"


# --- Inbound (WhatsApp Cloud API webhook) --------------------------------------------------------


def verify_signature(body: bytes, signature_header: str) -> bool:
    secret = settings.WHATSAPP_APP_SECRET
    if not secret or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.removeprefix("sha256="))


def store_whatsapp_webhook(payload: dict) -> list[InboundMessage]:
    """Store incoming messages (duplicates ignored) and apply delivery status updates."""
    stored = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                phone = str(msg.get("from", ""))
                customer = Customer.objects.filter(phone=phone).first()
                body = (msg.get("text") or {}).get("body", "") if msg.get("type") == "text" else ""
                try:
                    with transaction.atomic():
                        stored.append(
                            InboundMessage.objects.create(
                                provider_message_id=msg["id"],
                                from_phone=phone,
                                customer=customer,
                                body=body,
                                message_type=msg.get("type", "text"),
                                payload=msg,
                            )
                        )
                except IntegrityError:
                    continue  # redelivery
            for status in value.get("statuses", []):
                mapped = {
                    "sent": MessageStatus.SENT,
                    "delivered": MessageStatus.DELIVERED,
                    "read": MessageStatus.READ,
                    "failed": MessageStatus.FAILED,
                }.get(status.get("status"))
                if mapped:
                    OutboundMessage.objects.filter(provider_message_id=status.get("id")).update(status=mapped)
    return stored
