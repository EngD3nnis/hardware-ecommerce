from unittest import mock

import pytest

pytestmark = pytest.mark.django_db


class TestHealth:
    def test_live_is_ok_without_touching_dependencies(self, client):
        with mock.patch("apps.core.views.connection") as conn:
            response = client.get("/health/live")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        conn.cursor.assert_not_called()

    def test_ready_ok_when_database_reachable(self, client):
        response = client.get("/health/ready")
        assert response.status_code == 200
        # Test settings use an in-memory broker, so the Redis check is skipped.
        assert response.json() == {"status": "ok", "checks": {"database": "ok", "broker": "skipped"}}

    def test_ready_503_and_no_error_details_when_database_down(self, client, caplog):
        with mock.patch("apps.core.views.connection") as conn:
            conn.cursor.side_effect = RuntimeError("password authentication failed for user dewmix")
            response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["checks"]["database"] == "error"
        assert b"password" not in response.content
        assert "Readiness check failed: database" in caplog.text

    def test_ready_checks_redis_broker(self, client, settings):
        settings.CELERY_BROKER_URL = "redis://localhost:1/0"
        with mock.patch("apps.core.views.redis.Redis.from_url") as from_url:
            from_url.return_value.ping.side_effect = ConnectionError("refused")
            response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["checks"]["broker"] == "error"

    def test_health_rejects_non_get(self, client):
        assert client.post("/health/live").status_code == 405


class TestMetrics:
    def test_hidden_when_no_token_and_not_debug(self, client, settings):
        settings.METRICS_TOKEN = ""
        settings.DEBUG = False
        assert client.get("/metrics").status_code == 404

    def test_requires_bearer_token(self, client, settings):
        settings.METRICS_TOKEN = "scrape-secret"
        assert client.get("/metrics").status_code == 401
        assert client.get("/metrics", HTTP_AUTHORIZATION="Bearer wrong").status_code == 401

    def test_exports_with_valid_token(self, client, settings):
        settings.METRICS_TOKEN = "scrape-secret"
        response = client.get("/metrics", HTTP_AUTHORIZATION="Bearer scrape-secret")
        assert response.status_code == 200
        assert b"django_http_requests" in response.content
