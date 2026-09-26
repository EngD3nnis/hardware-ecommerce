"""Settings shared by every environment.

Nothing in this module may contain a usable secret or an insecure default.
Values that differ per environment (secrets, DEBUG, hosts, database) are set
in local.py / test.py / production.py. production.py refuses to start when
anything required is missing (see ADR 0005).
"""

from datetime import timedelta
from pathlib import Path

import environ
from celery.schedules import crontab
from django.core.exceptions import ImproperlyConfigured

# backend/
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
# Optional .env at the repository root (never committed; see .env.example).
# Real environment variables always win over values in the file.
# DJANGO_READ_DOT_ENV=0 skips it (used by tests that must control the environment).
if env.bool("DJANGO_READ_DOT_ENV", default=True):
    environ.Env.read_env(BASE_DIR.parent / ".env")

# --- Core -------------------------------------------------------------------

# Secrets: None here on purpose. Each environment module sets or requires them.
SECRET_KEY = env("DJANGO_SECRET_KEY", default=None)
JWT_SIGNING_KEY = env("JWT_SIGNING_KEY", default=None)

DEBUG = False
ALLOWED_HOSTS: list[str] = env.list("DJANGO_ALLOWED_HOSTS", default=[])
CSRF_TRUSTED_ORIGINS: list[str] = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    # Third-party
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "django_filters",
    "django_prometheus",
    "drf_spectacular",
    # Dewmix
    "apps.core",
    "apps.audit",
    "apps.authentication",
    "apps.catalog",
    "apps.pricing",
    "apps.inventory",
    "apps.procurement",
    "apps.customers",
    "apps.sales",
    "apps.payments",
    "apps.search",
    "apps.fulfillment",
    "apps.notifications",
    "apps.automation",
    "apps.ai",
]

MIDDLEWARE = [
    "django_prometheus.middleware.PrometheusBeforeMiddleware",
    "apps.core.middleware.RequestIDMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_prometheus.middleware.PrometheusAfterMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# --- Database ---------------------------------------------------------------

# PostgreSQL is the system of record (ADR 0002). DATABASES is built by each
# environment module with database_from_env().


def database_from_env(default_url: str | None = None) -> dict:
    """Parse DATABASE_URL, wrapping PostgreSQL with the Prometheus backend."""
    config = env.db("DATABASE_URL", default=default_url) if default_url else env.db("DATABASE_URL")
    if config["ENGINE"] == "django.db.backends.postgresql":
        config["ENGINE"] = "django_prometheus.db.backends.postgresql"
    config.setdefault("CONN_MAX_AGE", env.int("DATABASE_CONN_MAX_AGE", default=60))
    config.setdefault("CONN_HEALTH_CHECKS", True)
    return config


DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Auth -------------------------------------------------------------------

AUTH_USER_MODEL = "authentication.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- Internationalisation ---------------------------------------------------

LANGUAGE_CODE = "en-us"
USE_I18N = True
USE_TZ = True
# Timestamps are stored in UTC. Business-facing times (reports, quote expiry,
# opening hours) are computed in BUSINESS_TIMEZONE.
TIME_ZONE = "UTC"
BUSINESS_TIMEZONE = "Africa/Nairobi"

# --- Static & media ---------------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# Product images and documents. Local disk by default; S3-compatible object
# storage (e.g. Cloudflare R2) when a bucket is configured.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# --- Cache ------------------------------------------------------------------

# Used by API throttling. Redis when configured; per-process memory otherwise.
REDIS_CACHE_URL = env("REDIS_CACHE_URL", default="")
if REDIS_CACHE_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_CACHE_URL,
        }
    }
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# --- Django REST Framework --------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ("rest_framework_simplejwt.authentication.JWTAuthentication",),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 50,
    "EXCEPTION_HANDLER": "apps.core.exceptions.api_exception_handler",
    # Scoped throttles are opted into per view with `throttle_scope`.
    "DEFAULT_THROTTLE_RATES": {
        "auth": env("THROTTLE_RATE_AUTH", default="10/min"),
        "public": env("THROTTLE_RATE_PUBLIC", default="120/min"),
        "quote_request": env("THROTTLE_RATE_QUOTE_REQUEST", default="10/hour"),
    },
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Dewmix Platform API",
    "DESCRIPTION": "Catalogue, quotations, orders and operations API. Errors use the envelope described in README.md.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": r"/api/v1",
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=60),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,  # requires rest_framework_simplejwt.token_blacklist
    "UPDATE_LAST_LOGIN": True,
    "ALGORITHM": "HS256",
    # SIGNING_KEY is set by each environment module.
    "AUTH_HEADER_TYPES": ("Bearer",),
    "AUTH_TOKEN_CLASSES": ("rest_framework_simplejwt.tokens.AccessToken",),
}

# --- CORS -------------------------------------------------------------------

CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS: list[str] = env.list("CORS_ALLOWED_ORIGINS", default=[])

# --- Celery -----------------------------------------------------------------

CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6379/1")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="redis://localhost:6379/2")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = BUSINESS_TIMEZONE  # crontab times below are Nairobi time
CELERY_RESULT_EXPIRES = timedelta(days=1)
CELERY_TASK_TRACK_STARTED = True
# Acknowledge only after the task finishes, and re-queue if the worker dies,
# so work is not lost. This means tasks MUST be idempotent.
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
# Bounded execution: no task may run forever. Override per task where needed.
CELERY_TASK_SOFT_TIME_LIMIT = 5 * 60
CELERY_TASK_TIME_LIMIT = 6 * 60
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
# Periodic jobs (run `celery -A config beat`). Each task is idempotent.
CELERY_BEAT_SCHEDULE = {
    "release-expired-reservations": {
        "task": "apps.inventory.tasks.release_expired_reservations",
        "schedule": timedelta(minutes=10),
    },
    "expire-quotations": {"task": "apps.sales.tasks.expire_quotations", "schedule": timedelta(hours=1)},
    "process-stuck-payment-events": {
        "task": "apps.payments.tasks.process_stuck_payment_events",
        "schedule": timedelta(minutes=15),
    },
    "reconcile-inventory": {
        "task": "apps.automation.tasks.reconcile_inventory",
        "schedule": timedelta(hours=24),
    },
    "operational-checks": {"task": "apps.automation.tasks.run_operational_checks", "schedule": timedelta(minutes=30)},
    # Agents: skipped at no cost unless enabled in /ops/.
    "agent-inventory-daily": {
        "task": "apps.ai.tasks.run_scheduled_agent",
        "schedule": crontab(hour=6, minute=0),
        "args": ("inventory",),
    },
    "agent-operations-daily": {
        "task": "apps.ai.tasks.run_scheduled_agent",
        "schedule": crontab(hour=18, minute=30),
        "args": ("operations",),
    },
    "agent-catalogue-weekly": {
        "task": "apps.ai.tasks.run_scheduled_agent",
        "schedule": crontab(hour=7, minute=0, day_of_week="mon"),
        "args": ("catalogue",),
    },
}

# --- Business rules (configurable without code changes) ------------------------

# Purchase orders at or below this total (KES) are approved on submission when
# the submitter can approve. 0 = every purchase order needs explicit approval.
PURCHASE_AUTO_APPROVE_LIMIT = env.int("PURCHASE_AUTO_APPROVE_LIMIT", default=0)
# Separation of duties: the approver must not be the person who created the order.
PURCHASE_APPROVER_MUST_DIFFER = env.bool("PURCHASE_APPROVER_MUST_DIFFER", default=False)

# Standard Kenyan VAT rate, used to show the VAT included in VAT-inclusive prices.
VAT_RATE = env.float("VAT_RATE", default=0.16)

# --- M-Pesa (Safaricom Daraja STK push) ------------------------------------------
MPESA_ENVIRONMENT = env("MPESA_ENVIRONMENT", default="sandbox")  # sandbox | production
MPESA_CONSUMER_KEY = env("MPESA_CONSUMER_KEY", default="")
MPESA_CONSUMER_SECRET = env("MPESA_CONSUMER_SECRET", default="")
MPESA_SHORTCODE = env("MPESA_SHORTCODE", default="")
MPESA_PASSKEY = env("MPESA_PASSKEY", default="")
MPESA_TRANSACTION_TYPE = env("MPESA_TRANSACTION_TYPE", default="CustomerPayBillOnline")  # or CustomerBuyGoodsOnline
MPESA_CALLBACK_BASE_URL = env("MPESA_CALLBACK_BASE_URL", default="")  # public https base URL of this server
MPESA_CALLBACK_TOKEN = env("MPESA_CALLBACK_TOKEN", default="")  # long random secret, part of the callback URL
MPESA_CALLBACK_ALLOWED_IPS = env.list("MPESA_CALLBACK_ALLOWED_IPS", default=[])

# --- Customer messaging ------------------------------------------------------------
# Adapter per channel: console (log only), whatsapp_cloud, email, disabled.
NOTIFICATION_BACKENDS = {
    "WHATSAPP": env("NOTIFY_WHATSAPP_BACKEND", default="console"),
    "EMAIL": env("NOTIFY_EMAIL_BACKEND", default="console"),
    "SMS": env("NOTIFY_SMS_BACKEND", default="disabled"),
}
WHATSAPP_API_VERSION = env("WHATSAPP_API_VERSION", default="v21.0")
WHATSAPP_PHONE_NUMBER_ID = env("WHATSAPP_PHONE_NUMBER_ID", default="")
WHATSAPP_ACCESS_TOKEN = env("WHATSAPP_ACCESS_TOKEN", default="")
WHATSAPP_APP_SECRET = env("WHATSAPP_APP_SECRET", default="")  # verifies webhook signatures
WHATSAPP_VERIFY_TOKEN = env("WHATSAPP_VERIFY_TOKEN", default="")  # webhook subscription handshake
# Approved template names for business-initiated messages, by message kind, e.g. {"order_confirmed": "order_update"}
WHATSAPP_TEMPLATES = env.json("WHATSAPP_TEMPLATES", default={})
WHATSAPP_TEMPLATE_LANGUAGE = env("WHATSAPP_TEMPLATE_LANGUAGE", default="en")

# --- Object storage (S3-compatible, e.g. Cloudflare R2) ----------------------
# Wired up with django-storages in Stage 2.

AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID", default="")
AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY", default="")
AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME", default="")
AWS_S3_ENDPOINT_URL = env("AWS_S3_ENDPOINT_URL", default="")
AWS_S3_CUSTOM_DOMAIN = env("AWS_S3_CUSTOM_DOMAIN", default="") or None
AWS_S3_REGION_NAME = env("AWS_S3_REGION_NAME", default="auto")  # "auto" for Cloudflare R2
AWS_DEFAULT_ACL = None  # bucket policy decides; never make objects public-writable
AWS_QUERYSTRING_AUTH = env.bool("AWS_QUERYSTRING_AUTH", default=False)  # public product images
AWS_S3_FILE_OVERWRITE = False
AWS_S3_OBJECT_PARAMETERS = {"CacheControl": "public, max-age=31536000, immutable"}  # content-addressed
if AWS_STORAGE_BUCKET_NAME:
    STORAGES["default"] = {"BACKEND": "storages.backends.s3.S3Storage"}

# --- AI & automation (ADR 0004, 0011) ------------------------------------------
# Hard off-switch for every agent and automated tool call, independent of the
# database (use if the admin switch can't be reached). The core app ignores it.
AUTOMATION_HARD_DISABLE = env.bool("AUTOMATION_HARD_DISABLE", default=False)
# Anthropic server-side refusal fallbacks (the API retries a declined request on a fallback model).
AI_REFUSAL_FALLBACKS = env.bool("AI_REFUSAL_FALLBACKS", default=True)
# USD per 1M tokens (input, output), for cost estimates in the control centre.
AI_MODEL_PRICES = {
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
# Provider credentials. Only apps/ai/providers/ reads these.

ACTIVE_AI_PROVIDER = env("ACTIVE_AI_PROVIDER", default="")
OPENAI_API_KEY = env("OPENAI_API_KEY", default="")
GEMINI_API_KEY = env("GEMINI_API_KEY", default="")
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", default="")
LOCAL_LLM_URL = env("LOCAL_LLM_URL", default="")

# --- Observability ----------------------------------------------------------

# Bearer token required to scrape /metrics. Unset = endpoint disabled unless DEBUG.
METRICS_TOKEN = env("METRICS_TOKEN", default="")

LOG_LEVEL = env("LOG_LEVEL", default="INFO")
LOG_FORMAT = env("LOG_FORMAT", default="json")
if LOG_FORMAT not in ("json", "console"):
    raise ImproperlyConfigured(f"LOG_FORMAT must be 'json' or 'console', got {LOG_FORMAT!r}")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "correlation_id": {"()": "apps.core.log_formatting.CorrelationIdFilter"},
    },
    "formatters": {
        "json": {"()": "apps.core.log_formatting.JSONFormatter"},
        "console": {"format": "%(asctime)s %(levelname)-8s [%(correlation_id)s] %(name)s: %(message)s"},
    },
    "handlers": {
        "stdout": {
            "class": "logging.StreamHandler",
            "filters": ["correlation_id"],
            "formatter": LOG_FORMAT,
        },
    },
    "root": {"handlers": ["stdout"], "level": LOG_LEVEL},
    "loggers": {
        # Request errors are already reported by our handlers/Sentry; keep Django's at WARNING.
        "django": {"level": "WARNING", "propagate": True},
        "django.request": {"level": "ERROR", "propagate": True},
    },
}
