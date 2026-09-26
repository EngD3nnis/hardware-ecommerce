# 0005: Configuration fails closed in production

**Status:** Accepted, 2026-09-26

## Context

In the initial scaffold every entrypoint (`manage.py`, `wsgi.py`, `asgi.py`, Celery) defaulted to the *local* settings module. There, `DEBUG=True`, `ALLOWED_HOSTS=['*']` and all CORS origins are allowed. `SECRET_KEY` fell back to a string committed in the repository, and the same value signed JWTs. One missing environment variable on a server would therefore have served debug pages with settings and tracebacks to the public, and let anyone forge login tokens. Nothing would have warned anyone.

## Decision

- Server entrypoints (`wsgi.py`, `asgi.py`, `config/celery.py`) default to `config.settings.production`. Only `manage.py` defaults to local settings, for developer convenience.
- `base.py` contains no usable secrets and no insecure defaults.
- `production.py` raises `ImproperlyConfigured` at startup unless:
  - `DJANGO_SECRET_KEY` and `JWT_SIGNING_KEY` are both set, at least 50 characters, not `django-insecure…`, **and different from each other** (so either can be rotated independently);
  - `DJANGO_ALLOWED_HOSTS` is explicit and contains no `*`;
  - `DATABASE_URL` is set (no SQLite fallback).
- Optional integrations default to *off* rather than *open*. For example, `/metrics` returns 404 unless `METRICS_TOKEN` is set, and Sentry never sends PII.
- These guarantees are covered by tests (`config/tests/test_settings_guards.py`), and CI runs `check --deploy --fail-level WARNING` against production settings.

## Consequences

- A misconfigured server crashes at boot with a clear message instead of running insecurely. That is the intended trade-off.
- Developers must export `DJANGO_SETTINGS_MODULE=config.settings.local` when running Celery or Gunicorn locally (documented in the README).
- Rotating `JWT_SIGNING_KEY` logs out API clients without invalidating admin sessions or password-reset links, and vice versa.
