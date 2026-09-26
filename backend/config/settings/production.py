"""Production settings. Fails closed: missing or weak configuration stops startup.

wsgi.py, asgi.py and the Celery app default to this module (ADR 0005).
"""

import sentry_sdk
from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import (
    ALLOWED_HOSTS,
    JWT_SIGNING_KEY,
    SECRET_KEY,
    SIMPLE_JWT,
    database_from_env,
    env,
)

MIN_SECRET_LENGTH = 50


def _require_secret(name: str, value: str | None) -> str:
    if not value:
        raise ImproperlyConfigured(f"{name} must be set in production.")
    if len(value) < MIN_SECRET_LENGTH or len(set(value)) < 5 or value.startswith("django-insecure"):
        raise ImproperlyConfigured(f"{name} must be a random value of at least {MIN_SECRET_LENGTH} characters.")
    return value


DEBUG = False

SECRET_KEY = _require_secret("DJANGO_SECRET_KEY", SECRET_KEY)
# A separate key means rotating JWTs does not also invalidate sessions/password
# reset tokens, and vice versa.
SIMPLE_JWT["SIGNING_KEY"] = _require_secret("JWT_SIGNING_KEY", JWT_SIGNING_KEY)
if SIMPLE_JWT["SIGNING_KEY"] == SECRET_KEY:
    raise ImproperlyConfigured("JWT_SIGNING_KEY must differ from DJANGO_SECRET_KEY.")

if not ALLOWED_HOSTS or "*" in ALLOWED_HOSTS:
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must list the production host names (no '*').")

# No default: a missing DATABASE_URL raises instead of silently using SQLite.
DATABASES = {"default": database_from_env()}

# --- HTTPS / cookies (TLS terminates at the reverse proxy) -------------------

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
# Health checks come from inside the host over plain HTTP.
SECURE_REDIRECT_EXEMPT = [r"^health/"]
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31536000)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

# --- Error reporting --------------------------------------------------------

SENTRY_DSN = env("SENTRY_DSN", default="")
if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=env("SENTRY_ENVIRONMENT", default="production"),
        release=env("RELEASE_VERSION", default=None),
        traces_sample_rate=env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.1),
        # Customer phone numbers, IPs and cookies must not leave our systems
        # without a reason (Kenya Data Protection Act 2019).
        send_default_pii=False,
    )
