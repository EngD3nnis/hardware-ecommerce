"""Channel adapters: the integration boundary for sending messages.

Choose per channel with settings.NOTIFICATION_BACKENDS, e.g.
{"WHATSAPP": "whatsapp_cloud", "EMAIL": "email", "SMS": "disabled"}.
Swapping a provider means adding an adapter here; business code never changes.
"""

import logging
from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


class TransientSendError(Exception):
    """Worth retrying (network, 5xx, rate limit)."""


class PermanentSendError(Exception):
    """Retrying won't help (bad number, not allowed, misconfigured)."""


@dataclass(frozen=True)
class SendResult:
    provider_message_id: str | None


class ConsoleAdapter:
    """Development: log the message instead of sending it."""

    name = "console"

    def send(self, message) -> SendResult:
        logger.info(
            "Message (console backend)", extra={"to": message.to, "template": message.template, "body": message.body}
        )
        return SendResult(provider_message_id=None)


class DisabledAdapter:
    name = "disabled"

    def send(self, message) -> SendResult:
        raise PermanentSendError(f"The {message.channel} channel is disabled on this server.")


class EmailAdapter:
    name = "email"

    def send(self, message) -> SendResult:
        try:
            send_mail(f"{message.template.replace('_', ' ').title()}", message.body, None, [message.to])
        except OSError as exc:
            raise TransientSendError(str(exc)) from exc
        return SendResult(provider_message_id=None)


class WhatsAppCloudAdapter:
    """Meta WhatsApp Business Cloud API: free-text messages.

    Note: outside the 24-hour customer-service window WhatsApp only allows
    pre-approved templates. Those are sent with template_name in settings;
    the text body is then passed as the template's first parameter.
    """

    name = "whatsapp_cloud"

    def send(self, message) -> SendResult:
        if not (settings.WHATSAPP_PHONE_NUMBER_ID and settings.WHATSAPP_ACCESS_TOKEN):
            raise PermanentSendError("WhatsApp Cloud API is not configured.")
        url = f"https://graph.facebook.com/{settings.WHATSAPP_API_VERSION}/{settings.WHATSAPP_PHONE_NUMBER_ID}/messages"
        template = settings.WHATSAPP_TEMPLATES.get(message.template)
        if template:
            payload = {
                "messaging_product": "whatsapp",
                "to": message.to,
                "type": "template",
                "template": {
                    "name": template,
                    "language": {"code": settings.WHATSAPP_TEMPLATE_LANGUAGE},
                    "components": [{"type": "body", "parameters": [{"type": "text", "text": message.body[:1000]}]}],
                },
            }
        else:
            payload = {
                "messaging_product": "whatsapp",
                "to": message.to,
                "type": "text",
                "text": {"body": message.body[:4096], "preview_url": True},
            }
        try:
            response = requests.post(
                url, json=payload, timeout=15, headers={"Authorization": f"Bearer {settings.WHATSAPP_ACCESS_TOKEN}"}
            )
        except requests.RequestException as exc:
            raise TransientSendError(str(exc)) from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise TransientSendError(f"WhatsApp API {response.status_code}")
        if response.status_code >= 400:
            raise PermanentSendError(f"WhatsApp API {response.status_code}: {response.text[:300]}")
        messages = response.json().get("messages") or [{}]
        return SendResult(provider_message_id=messages[0].get("id"))


ADAPTERS = {a.name: a for a in (ConsoleAdapter(), DisabledAdapter(), EmailAdapter(), WhatsAppCloudAdapter())}


def adapter_for(channel: str):
    name = settings.NOTIFICATION_BACKENDS.get(channel, "disabled")
    return ADAPTERS[name]
