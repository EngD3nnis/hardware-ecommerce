"""Settings for the automated test suite (pytest sets this module)."""

from .base import *  # noqa: F403
from .base import SIMPLE_JWT, database_from_env

DEBUG = False

SECRET_KEY = "test-only-secret-key-not-used-anywhere-else-0123456789abcdef"  # noqa: S105
SIMPLE_JWT["SIGNING_KEY"] = SECRET_KEY

ALLOWED_HOSTS = ["testserver", "localhost"]

# Tests run against PostgreSQL, like production (ADR 0002).
DATABASES = {"default": database_from_env("postgres://dewmix:dewmix@localhost:5432/dewmix")}
DATABASES["default"]["CONN_MAX_AGE"] = 0

CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
