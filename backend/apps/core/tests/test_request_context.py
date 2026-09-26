import json
import logging

import pytest

from apps.core.log_formatting import CorrelationIdFilter, JSONFormatter
from apps.core.request_context import get_correlation_id, set_correlation_id
from config import celery as celery_module

pytestmark = pytest.mark.django_db


class TestRequestIDMiddleware:
    def test_generates_request_id(self, client):
        response = client.get("/health/live")
        request_id = response["X-Request-ID"]
        assert len(request_id) == 32

    def test_reuses_valid_incoming_id(self, client):
        response = client.get("/health/live", HTTP_X_REQUEST_ID="proxy-abc.123")
        assert response["X-Request-ID"] == "proxy-abc.123"

    @pytest.mark.parametrize("bad", ["", "has spaces", "x" * 129, "inject\r\nheader", "<script>"])
    def test_replaces_unsafe_incoming_id(self, client, bad):
        response = client.get("/health/live", HTTP_X_REQUEST_ID=bad)
        assert response["X-Request-ID"] != bad
        assert len(response["X-Request-ID"]) == 32

    def test_context_cleared_after_request(self, client):
        client.get("/health/live")
        assert get_correlation_id() is None


class TestLogging:
    def _record(self, **extra):
        record = logging.makeLogRecord(
            {"name": "dewmix.test", "levelname": "INFO", "msg": "hello %s", "args": ("world",)}
        )
        record.__dict__.update(extra)
        CorrelationIdFilter().filter(record)
        return record

    def test_json_contains_message_correlation_id_and_extra(self):
        set_correlation_id("abc123")
        try:
            line = JSONFormatter().format(self._record(order_id="O-1"))
        finally:
            set_correlation_id(None)
        payload = json.loads(line)
        assert payload["message"] == "hello world"
        assert payload["correlation_id"] == "abc123"
        assert payload["order_id"] == "O-1"
        assert payload["level"] == "INFO"

    def test_missing_correlation_id_is_dash(self):
        payload = json.loads(JSONFormatter().format(self._record()))
        assert payload["correlation_id"] == "-"


class TestCeleryCorrelation:
    def test_publisher_attaches_current_id(self):
        headers = {}
        set_correlation_id("req-1")
        try:
            celery_module._attach_correlation_id(headers=headers)
        finally:
            set_correlation_id(None)
        assert headers == {"correlation_id": "req-1"}

    def test_worker_restores_id_then_clears(self):
        class FakeRequest:
            id = "task-id"
            correlation_id = "req-1"

        class FakeTask:
            request = FakeRequest()

        celery_module._restore_correlation_id(task=FakeTask())
        assert get_correlation_id() == "req-1"
        celery_module._clear_correlation_id()
        assert get_correlation_id() is None

    def test_worker_falls_back_to_task_id(self):
        class FakeRequest:
            id = "task-id"

        class FakeTask:
            request = FakeRequest()

        celery_module._restore_correlation_id(task=FakeTask())
        try:
            assert get_correlation_id() == "task-id"
        finally:
            celery_module._clear_correlation_id()
