"""Production settings must fail closed.

Each case loads settings in a fresh interpreter with a controlled environment,
because Django settings can only be configured once per process.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]

GOOD_SECRET = "s" * 20 + "0123456789abcdefghijklmnopqrstuvwxyz"
GOOD_JWT = "j" * 20 + "zyxwvutsrqponmlkjihgfedcba9876543210"
VALID_ENV = {
    "DJANGO_SETTINGS_MODULE": "config.settings.production",
    "DJANGO_SECRET_KEY": GOOD_SECRET,
    "JWT_SIGNING_KEY": GOOD_JWT,
    "DJANGO_ALLOWED_HOSTS": "dewmixhardware.com",
    "DATABASE_URL": "postgres://u:p@db.internal:5432/dewmix",
}


def load_settings(
    env: dict, code: str = "from django.conf import settings; settings.DEBUG"
) -> subprocess.CompletedProcess:
    clean = {k: v for k, v in os.environ.items() if not k.startswith(("DJANGO_", "JWT_", "DATABASE_"))}
    clean["DJANGO_READ_DOT_ENV"] = "0"  # ignore any developer .env file
    clean.update(env)
    return subprocess.run(  # noqa: S603 - fixed interpreter and code
        [sys.executable, "-c", code],
        cwd=BACKEND_DIR,
        env=clean,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_valid_production_config_loads_with_debug_off():
    result = load_settings(
        VALID_ENV, "from django.conf import settings; print(settings.DEBUG, settings.SECURE_SSL_REDIRECT)"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False True"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"DJANGO_SECRET_KEY": ""}, "DJANGO_SECRET_KEY must be set"),
        ({"DJANGO_SECRET_KEY": "short"}, "DJANGO_SECRET_KEY must be a random value"),
        ({"DJANGO_SECRET_KEY": "django-insecure-" + "x" * 60}, "DJANGO_SECRET_KEY must be a random value"),
        ({"JWT_SIGNING_KEY": ""}, "JWT_SIGNING_KEY must be set"),
        ({"JWT_SIGNING_KEY": GOOD_SECRET}, "JWT_SIGNING_KEY must differ"),
        ({"DJANGO_ALLOWED_HOSTS": ""}, "DJANGO_ALLOWED_HOSTS"),
        ({"DJANGO_ALLOWED_HOSTS": "*"}, "DJANGO_ALLOWED_HOSTS"),
        ({"DATABASE_URL": ""}, "DATABASE_URL"),
    ],
)
def test_production_refuses_to_start_with_bad_config(overrides, message):
    env = {**VALID_ENV, **overrides}
    env = {k: v for k, v in env.items() if v != ""}  # "" means: variable not set
    result = load_settings(env)
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr
    assert message in result.stderr


def test_server_entrypoints_default_to_production_settings():
    """Forgetting DJANGO_SETTINGS_MODULE on a server must not silently enable DEBUG."""
    env = {k: v for k, v in VALID_ENV.items() if k != "DJANGO_SETTINGS_MODULE"}
    code = "import config.wsgi; from django.conf import settings; print(settings.SETTINGS_MODULE, settings.DEBUG)"
    result = load_settings(env, code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "config.settings.production False"


def test_celery_defaults_to_production_settings():
    env = {k: v for k, v in VALID_ENV.items() if k != "DJANGO_SETTINGS_MODULE"}
    code = "from config.celery import app; import os; print(os.environ['DJANGO_SETTINGS_MODULE'])"
    result = load_settings(env, code)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "config.settings.production"


def test_sentry_does_not_send_pii():
    source = (BACKEND_DIR / "config/settings/production.py").read_text()
    assert "send_default_pii=False" in source
