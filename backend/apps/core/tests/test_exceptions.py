import pytest
from django.http import Http404
from rest_framework import serializers
from rest_framework.exceptions import NotAuthenticated, Throttled

from apps.core import exceptions as exc
from apps.core.exceptions import api_exception_handler
from apps.core.request_context import set_correlation_id


@pytest.fixture(autouse=True)
def _request_id():
    set_correlation_id("req-42")
    yield
    set_correlation_id(None)


def handle(error):
    return api_exception_handler(error, context={})


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (exc.ValidationError("Quantity must be positive.", details={"qty": -1}), 400, "validation_error"),
        (exc.NotFound(), 404, "not_found"),
        (exc.PermissionDenied(), 403, "permission_denied"),
        (exc.Conflict("Order already dispatched."), 409, "conflict"),
    ],
)
def test_domain_errors_use_envelope(error, status, code):
    response = handle(error)
    assert response.status_code == status
    assert response.data["error"]["code"] == code
    assert response.data["error"]["message"] == error.message
    assert response.data["error"]["request_id"] == "req-42"


def test_domain_error_details_are_returned():
    response = handle(exc.ValidationError("bad", details={"qty": -1}))
    assert response.data["error"]["details"] == {"qty": -1}


def test_invariant_violation_is_logged_as_error(caplog):
    response = handle(exc.InvariantViolation("reserved > on_hand"))
    assert response.status_code == 409
    assert any(r.levelname == "ERROR" and "reserved > on_hand" in r.getMessage() for r in caplog.records)


def test_drf_validation_error_keeps_field_details():
    response = handle(serializers.ValidationError({"email": ["This field is required."]}))
    assert response.status_code == 400
    assert response.data["error"]["code"] == "invalid"
    assert response.data["error"]["details"] == {"email": ["This field is required."]}


def test_drf_auth_error_keeps_status_and_code():
    response = handle(NotAuthenticated())
    assert response.status_code == 401
    assert response.data["error"]["code"] == "not_authenticated"


def test_throttle_keeps_retry_after_header():
    response = handle(Throttled(wait=30))
    assert response.status_code == 429
    assert response["Retry-After"] == "30"


def test_django_404_gets_not_found_code():
    response = handle(Http404("secret internal path"))
    assert response.status_code == 404
    assert response.data["error"]["code"] == "not_found"
    assert "secret" not in str(response.data)


def test_unexpected_exception_is_generic_500_and_logged(settings, caplog):
    settings.DEBUG = False
    response = handle(KeyError("db_password=hunter2"))
    assert response.status_code == 500
    assert response.data["error"]["code"] == "server_error"
    assert "hunter2" not in str(response.data)
    assert "Unhandled exception in API view" in caplog.text


def test_unexpected_exception_propagates_in_debug(settings):
    settings.DEBUG = True
    assert handle(KeyError("x")) is None
