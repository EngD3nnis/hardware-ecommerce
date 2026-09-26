"""Per-request / per-task correlation id.

One id follows a unit of work (an HTTP request, and any Celery tasks it
enqueues) through logs, audit events and error reports, so a failure can be
traced end to end.

Deliberately free of Django imports: config/celery.py imports it before
Django is set up.
"""

import re
import uuid
from contextvars import ContextVar

# Name of the Celery message header that carries the id between processes.
CORRELATION_HEADER = "correlation_id"

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

# Accept caller-supplied ids only if they are short and boring, so they are
# safe to put in logs and response headers.
_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def new_correlation_id() -> str:
    return uuid.uuid4().hex


def is_valid_correlation_id(value: str | None) -> bool:
    return bool(value) and bool(_VALID_ID.match(value))


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def set_correlation_id(value: str | None) -> None:
    _correlation_id.set(value)
