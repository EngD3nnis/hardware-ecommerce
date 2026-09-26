from .request_context import (
    ClientMeta,
    is_valid_correlation_id,
    new_correlation_id,
    set_client_meta,
    set_correlation_id,
)

REQUEST_ID_HEADER = "X-Request-ID"


def client_ip(request) -> str | None:
    """REMOTE_ADDR, or the address our own reverse proxy reports.

    X-Forwarded-For is only trusted when the direct peer is a configured proxy
    (settings.TRUSTED_PROXY_IPS); then its right-most entry, the one our proxy
    appended, is used. Clients can forge the left-hand entries.
    """
    from django.conf import settings

    remote = request.META.get("REMOTE_ADDR") or None
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if remote and forwarded and remote in settings.TRUSTED_PROXY_IPS:
        return forwarded.split(",")[-1].strip() or remote
    return remote


class RequestIDMiddleware:
    """Assign every request a correlation id and echo it in the response.

    A valid incoming X-Request-ID (e.g. from the reverse proxy) is reused so
    proxy logs and application logs line up; otherwise a new id is generated.
    Also records the client IP / user agent for audit events.

    See client_ip() for how the client address is determined behind a proxy.
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
                ip=client_ip(request),
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
