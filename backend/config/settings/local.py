"""Local development. Never use in production.

`manage.py` defaults to this module. The servers (wsgi/asgi) and Celery default
to production settings instead, so a missing DJANGO_SETTINGS_MODULE on a server
fails closed.
"""

from .base import *  # noqa: F403
from .base import SIMPLE_JWT, database_from_env, env

DEBUG = True

# Development-only fallbacks. production.py never has these.
# `or` also covers a key present in .env but left empty.
SECRET_KEY = env("DJANGO_SECRET_KEY", default="") or "dev-only-insecure-secret-key-do-not-use-in-production"
SIMPLE_JWT["SIGNING_KEY"] = env("JWT_SIGNING_KEY", default="") or SECRET_KEY

ALLOWED_HOSTS = ["localhost", "127.0.0.1", "0.0.0.0"]  # noqa: S104 - dev server only

# The static site is served from a different local port during development.
CORS_ALLOWED_ORIGINS = env.list(
    "CORS_ALLOWED_ORIGINS",
    default=["http://localhost:8080", "http://127.0.0.1:8080"],
)

# Matches docker-compose.yml. Override with DATABASE_URL.
DATABASES = {"default": database_from_env("postgres://dewmix:dewmix@localhost:5432/dewmix")}

LOGGING["handlers"]["stdout"]["formatter"] = env("LOG_FORMAT", default="console")  # noqa: F405

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
