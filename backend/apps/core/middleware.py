from .request_context import (
    ClientMeta,
    is_valid_correlation_id,
    new_correlation_id,
    set_client_meta,
    set_correlation_id,
)

REQUEST_ID_HEADER = "X-Request-ID"


class RequestIDMiddleware:
    """Assign every request a correlation id and echo it in the response.

    A valid incoming X-Request-ID (e.g. from the reverse proxy) is reused so
    proxy logs and application logs line up; otherwise a new id is generated.
    Also records the client IP / user agent for audit events.

    The IP is REMOTE_ADDR. Behind a reverse proxy, the proxy must set it to the
    real client address (Caddy/nginx "trusted proxy" config); X-Forwarded-For
    is not trusted here because clients can forge it.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = incoming if is_valid_correlation_id(incoming) else new_correlation_id()

        request.request_id = request_id
        set_correlation_id(request_id)
        set_client_meta(
            ClientMeta(
                ip=request.META.get("REMOTE_ADDR") or None,
                user_agent=request.headers.get("User-Agent", "")[:300],
            )
        )
        try:
            response = self.get_response(request)
        finally:
            set_correlation_id(None)
            set_client_meta(None)

        response[REQUEST_ID_HEADER] = request_id
        return response
