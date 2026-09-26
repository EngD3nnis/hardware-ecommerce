from .request_context import (
    is_valid_correlation_id,
    new_correlation_id,
    set_correlation_id,
)

REQUEST_ID_HEADER = "X-Request-ID"


class RequestIDMiddleware:
    """Assign every request a correlation id and echo it in the response.

    A valid incoming X-Request-ID (e.g. from the reverse proxy) is reused so
    proxy logs and application logs line up; otherwise a new id is generated.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = incoming if is_valid_correlation_id(incoming) else new_correlation_id()

        request.request_id = request_id
        set_correlation_id(request_id)
        try:
            response = self.get_response(request)
        finally:
            set_correlation_id(None)

        response[REQUEST_ID_HEADER] = request_id
        return response
