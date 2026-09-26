"""M-Pesa callback endpoint: authenticate cheaply, store, acknowledge, process in the background."""

import hmac
import json
import logging

from django.conf import settings
from django.db import transaction
from django.http import Http404, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import services, tasks

logger = logging.getLogger(__name__)
MAX_BODY = 64 * 1024


@csrf_exempt  # server-to-server call from Safaricom; authenticated by the URL token below
@require_POST
def mpesa_callback(request, token: str):
    expected = settings.MPESA_CALLBACK_TOKEN
    if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
        raise Http404  # don't reveal that the endpoint exists
    ip = request.META.get("REMOTE_ADDR")
    allowed = settings.MPESA_CALLBACK_ALLOWED_IPS
    if allowed and ip not in allowed:
        logger.warning("M-Pesa callback from unexpected IP", extra={"ip": ip})
        raise Http404
    if len(request.body) > MAX_BODY:
        return JsonResponse({"ResultCode": 1, "ResultDesc": "Too large"}, status=413)
    try:
        payload = json.loads(request.body)
    except ValueError:
        return JsonResponse({"ResultCode": 1, "ResultDesc": "Invalid JSON"}, status=400)
    event, created = services.store_mpesa_callback(payload, ip)
    if created:
        event_id = event.pk
        transaction.on_commit(lambda: tasks.process_payment_event.delay(event_id))
    # Always acknowledge a stored (or duplicate) event so Safaricom stops retrying.
    return JsonResponse({"ResultCode": 0, "ResultDesc": "Accepted"})
