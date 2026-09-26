"""Settings shared by every environment.

Nothing in this module may contain a usable secret or an insecure default.
Values that differ per environment (secrets, DEBUG, hosts, database) are set
in local.py / test.py / production.py. production.py refuses to start when
anything required is missing (see ADR 0005).
"""

from datetime import timedelta
from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

# backend/
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
# Optional .env at the repository root (never committed; see .env.example).
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
    # Third-party
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt",
    "rest_framework_simplejwt.token_blacklist",
    "django_filters",
    "django_prometheus",
    # Dewmix
    "apps.core",
    "apps.authentication",
    "apps.catalog",
    "apps.inventory",
    "apps.orders",
    "apps.payments",
    "apps.communications",
    "apps.ai_service",
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
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 50,
    "EXCEPTION_HANDLER": "apps.core.exceptions.api_exception_handler",
    # Scoped throttles are opted into per view with `throttle_scope`.
    "DEFAULT_THROTTLE_RATES": {
        "auth": env("THROTTLE_RATE_AUTH", default="10/min"),
    },
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
CELERY_TIMEZONE = TIME_ZONE
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

# --- Object storage (S3-compatible, e.g. Cloudflare R2) ----------------------
# Wired up with django-storages in Stage 2.

AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID", default="")
AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY", default="")
AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME", default="")
AWS_S3_ENDPOINT_URL = env("AWS_S3_ENDPOINT_URL", default="")
AWS_S3_CUSTOM_DOMAIN = env("AWS_S3_CUSTOM_DOMAIN", default="")

# --- AI providers -----------------------------------------------------------
# Read only by the AI provider layer (Stage 8). Business code never uses these.

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
