import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

CORRELATION_ID_HEADER = "X-Correlation-ID"


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Attaches a per-request correlation ID (from the incoming header if
    present, otherwise freshly generated) to request.state and the response,
    so audit records and logs can be tied back to a single request."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        correlation_id = request.headers.get(CORRELATION_ID_HEADER) or uuid.uuid4().hex
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers[CORRELATION_ID_HEADER] = correlation_id
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds defence-in-depth response headers: a Content-Security-Policy,
    clickjacking protection (X-Frame-Options + frame-ancestors in the CSP),
    MIME-sniffing protection, a referrer policy, and HSTS when the deployment
    is behind HTTPS. Configured via Settings so development stays permissive."""

    def __init__(self, app, *, csp: str, behind_https: bool) -> None:
        super().__init__(app)
        self._csp = csp
        self._behind_https = behind_https

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        headers = response.headers
        headers.setdefault("Content-Security-Policy", self._csp)
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
        if self._behind_https:
            headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    """Rejects oversized request bodies (by Content-Length) with 413 before the
    handler runs — a cheap upload/DoS guard. Streamed bodies without a length
    are left to the server's own limits."""

    def __init__(self, app, *, max_bytes: int) -> None:
        super().__init__(app)
        self._max_bytes = max_bytes

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self._max_bytes:
                    return Response("Request entity too large", status_code=413)
            except ValueError:
                return Response("Invalid Content-Length", status_code=400)
        return await call_next(request)
