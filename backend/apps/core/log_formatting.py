"""Logging helpers referenced from settings.LOGGING."""

import json
import logging
from datetime import UTC, datetime

from .request_context import get_correlation_id

# Attributes every LogRecord has; anything else was passed via `extra=` and is
# included in the JSON output.
_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "correlation_id"}


class CorrelationIdFilter(logging.Filter):
    """Stamp each record with the current correlation id (or "-")."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id() or "-"
        return True


class JSONFormatter(logging.Formatter):
    """One JSON object per line, for log shippers and grep-with-jq."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", "-"),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)
