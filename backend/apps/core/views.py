"""Operational endpoints: health checks and the Prometheus metrics export.

These are plain Django views (not DRF) so they do not depend on API
authentication settings and stay cheap.
"""

import hmac
import logging

import redis
from django.conf import settings
from django.db import connection
from django.http import Http404, HttpResponse, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from django_prometheus.exports import ExportToDjangoView

logger = logging.getLogger(__name__)


@never_cache
@require_GET
def health_live(request):
    """The process is up and serving requests. Checks no dependencies."""
    return JsonResponse({"status": "ok"})


@never_cache
@require_GET
def health_ready(request):
    """Dependencies needed to serve traffic are reachable.

    Only check names and ok/error are returned; failure details go to the logs.
    """
    checks = {"database": _check_database(), "broker": _check_broker()}
    healthy = all(result in ("ok", "skipped") for result in checks.values())
    return JsonResponse({"status": "ok" if healthy else "error", "checks": checks}, status=200 if healthy else 503)


def _check_database() -> str:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001 - any failure means not ready; logged with traceback
        logger.exception("Readiness check failed: database")
        return "error"
    return "ok"


def _check_broker() -> str:
    url = settings.CELERY_BROKER_URL
    if not url.startswith(("redis://", "rediss://")):
        return "skipped"
    try:
        client = redis.Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        client.ping()
    except Exception:  # noqa: BLE001 - any failure means not ready; logged with traceback
        logger.exception("Readiness check failed: broker")
        return "error"
    return "ok"


@never_cache
@require_GET
def metrics(request):
    """Prometheus scrape endpoint, protected by a bearer token.

    With METRICS_TOKEN unset the endpoint is only reachable when DEBUG is on,
    so metrics are never accidentally public in production.
    """
    token = settings.METRICS_TOKEN
    if not token:
        if not settings.DEBUG:
            raise Http404
    else:
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(supplied.encode(), token.encode()):
            return HttpResponse(status=401, headers={"WWW-Authenticate": "Bearer"})
    return ExportToDjangoView(request)
