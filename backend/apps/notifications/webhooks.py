"""WhatsApp Cloud API webhook: GET = subscription verification, POST = messages/statuses (signed)."""

import hmac
import json
import logging

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from . import services

logger = logging.getLogger(__name__)


@csrf_exempt  # signed by Meta with the app secret (X-Hub-Signature-256)
def whatsapp_webhook(request):
    if request.method == "GET":
        token = request.GET.get("hub.verify_token", "")
        expected = settings.WHATSAPP_VERIFY_TOKEN
        if request.GET.get("hub.mode") == "subscribe" and expected and hmac.compare_digest(token, expected):
            return HttpResponse(request.GET.get("hub.challenge", ""), content_type="text/plain")
        return HttpResponseForbidden()
    if request.method != "POST":
        return HttpResponse(status=405)
    if not services.verify_signature(request.body, request.headers.get("X-Hub-Signature-256", "")):
        logger.warning("WhatsApp webhook with bad signature")
        return HttpResponseForbidden()
    try:
        payload = json.loads(request.body)
    except ValueError:
        return JsonResponse({"error": "invalid json"}, status=400)
    stored = services.store_whatsapp_webhook(payload)
    if stored:
        # Hand new messages to the sales agent (a no-op unless it is enabled). Lazy import: ai depends on notifications.
        from apps.ai.tasks import handle_inbound_message

        ids = [m.pk for m in stored]
        transaction.on_commit(lambda: [handle_inbound_message.delay(i) for i in ids])
    return JsonResponse({"status": "ok"})
