"""Application exceptions and the API error envelope.

Services raise DomainError subclasses; they never return error codes or
None-on-failure. The DRF exception handler below turns them (and DRF's own
exceptions) into one consistent response shape:

    {"error": {"code": "...", "message": "...", "details": {...}, "request_id": "..."}}

Unexpected exceptions are logged with a traceback and returned as a generic
500. Stack traces and exception messages never reach the client.
"""

import logging

from django.conf import settings
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework import exceptions as drf_exceptions
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from .request_context import get_correlation_id

logger = logging.getLogger(__name__)

# Headers DRF sets on error responses that clients rely on.
_FORWARDED_HEADERS = ("Retry-After", "WWW-Authenticate", "Allow")


class DomainError(Exception):
    """Base class for expected, business-level failures."""

    code = "domain_error"
    http_status = status.HTTP_400_BAD_REQUEST
    default_message = "The request could not be completed."

    def __init__(self, message: str | None = None, *, details: dict | None = None):
        self.message = message or self.default_message
        self.details = details or {}
        super().__init__(self.message)


class ValidationError(DomainError):
    code = "validation_error"
    http_status = status.HTTP_400_BAD_REQUEST
    default_message = "Invalid input."


class NotFound(DomainError):
    code = "not_found"
    http_status = status.HTTP_404_NOT_FOUND
    default_message = "Not found."


class PermissionDenied(DomainError):
    code = "permission_denied"
    http_status = status.HTTP_403_FORBIDDEN
    default_message = "You do not have permission to perform this action."


class Conflict(DomainError):
    """The request clashes with current state (e.g. illegal status transition)."""

    code = "conflict"
    http_status = status.HTTP_409_CONFLICT
    default_message = "The request conflicts with the current state."


class InvariantViolation(DomainError):
    """A business rule that must never be broken would have been broken.

    Signals a bug or a race, not bad user input, so it is also logged as an error.
    """

    code = "invariant_violation"
    http_status = status.HTTP_409_CONFLICT
    default_message = "The operation would violate a business rule."


def _error_response(code: str, message: str, http_status: int, details=None, headers=None) -> Response:
    body = {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": get_correlation_id(),
        }
    }
    return Response(body, status=http_status, headers=headers)


def api_exception_handler(exc, context):
    """DRF EXCEPTION_HANDLER: see settings.REST_FRAMEWORK."""
    if isinstance(exc, DomainError):
        if isinstance(exc, InvariantViolation):
            logger.error("Invariant violation: %s", exc.message, extra={"details": exc.details})
        return _error_response(exc.code, exc.message, exc.http_status, exc.details)

    # Same conversion DRF does internally, done here so we keep a proper error code.
    if isinstance(exc, Http404):
        exc = drf_exceptions.NotFound()
    elif isinstance(exc, DjangoPermissionDenied):
        exc = drf_exceptions.PermissionDenied()

    response = drf_exception_handler(exc, context)
    if response is not None:
        # DRF's own exceptions (auth, throttling, serializer validation, 404, 405…).
        if isinstance(exc, drf_exceptions.ValidationError):
            message, details = "Invalid input.", response.data
        else:
            message, details = str(getattr(exc, "detail", exc)), {}
        # Prefer the specific code on the detail (e.g. simplejwt's "no_active_account").
        code = getattr(exc.detail, "code", None) or exc.default_code
        headers = {h: response[h] for h in _FORWARDED_HEADERS if h in response}
        return _error_response(code, message, response.status_code, details, headers)

    if settings.DEBUG:
        return None  # let Django render its debug page locally

    logger.exception("Unhandled exception in API view")
    return _error_response(
        "server_error",
        "An unexpected error occurred. Please try again or contact support with the request id.",
        status.HTTP_500_INTERNAL_SERVER_ERROR,
    )
