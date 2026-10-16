from urllib.parse import quote

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy.exc import DataError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

logger = get_logger("errors")


class AppError(Exception):
    """Base application error mapped to a JSON error response."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class NotFoundError(AppError):
    def __init__(self, message: str = "Resource not found") -> None:
        super().__init__(message, status_code=404)


class NotAuthenticatedError(AppError):
    """Raised when a request has no valid session. Browser navigations get
    redirected to the login page; API/XHR-style requests still get a plain 401
    (see not_authenticated_handler)."""

    def __init__(self, message: str = "Not authenticated") -> None:
        super().__init__(message, status_code=401)


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def _safe_next_path(request: Request) -> str:
    path = request.url.path
    if request.url.query:
        path = f"{path}?{request.url.query}"
    return path


# What a visitor reads for each status. A browser gets a page with a way back
# rather than a bare {"detail": ...}; API callers keep the JSON they rely on.
_PAGE_COPY = {
    400: ("Something was wrong with that request", None),
    403: ("You don't have access to this page", None),
    404: (
        "Page not found",
        "The page may have moved, or the event may have already happened and been "
        "taken down.",
    ),
    405: ("Page not found", None),
    429: ("Too many attempts", None),
    500: (
        "Something went wrong on our end",
        "It has been logged. Please try again in a moment.",
    ),
}


def error_page(request: Request, status_code: int, message: str | None = None) -> Response:
    """The HTML error page for a browser request. The handler's own message is
    shown for 4xx (it is written for people, e.g. "Event not found"), never for
    a 500, whose details stay in the log."""
    from app.core.templating import render  # templating imports app services

    title, explanation = _PAGE_COPY.get(status_code, _PAGE_COPY[500 if status_code >= 500 else 400])
    if status_code < 500 and message and not explanation:
        explanation = message
    return render(
        request,
        "error.html",
        {"status_code": status_code, "title": title, "explanation": explanation},
        status_code=status_code,
    )


async def app_error_handler(request: Request, exc: AppError) -> Response:
    if _wants_html(request):
        return error_page(request, exc.status_code, exc.message)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
    """Framework-raised errors: chiefly a URL that matches no route (404) or a
    route called with the wrong method (405)."""
    if _wants_html(request):
        return error_page(request, exc.status_code)
    return JSONResponse(
        status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers
    )


async def request_validation_handler(request: Request, exc: RequestValidationError) -> Response:
    """A request whose URL or form does not parse, e.g. /events/abc. A browser
    gets the friendly page: a malformed id in the path is a page that does not
    exist (404), anything else a bad request (400). API callers keep FastAPI's
    usual 422 with the field details."""
    if _wants_html(request):
        in_path = any(error.get("loc", ())[:1] == ("path",) for error in exc.errors())
        return error_page(request, 404 if in_path else 400)
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})


async def overflow_handler(request: Request, exc: OverflowError) -> Response:
    """SQLite refuses integers past 64 bits, so a URL like /events/99999999999999999999
    fails inside the query. No row can have such an id: answer as not found."""
    if "SQLite INTEGER" not in str(exc):
        return await unhandled_exception_handler(request, exc)
    return _client_error(request, 404, "Not found")


async def data_error_handler(request: Request, exc: DataError) -> Response:
    """PostgreSQL's counterparts of the above: an id past its integer range is
    not found, and a NUL byte (which text columns cannot hold) is a bad
    request. Any other DataError is a real bug and is reported as one."""
    reason = type(getattr(exc, "orig", None)).__name__
    if reason == "NumericValueOutOfRange":
        return _client_error(request, 404, "Not found")
    if "NUL (0x00)" in str(exc.orig):
        return _client_error(request, 400, "Bad request")
    return await unhandled_exception_handler(request, exc)


def _client_error(request: Request, status_code: int, detail: str) -> Response:
    if _wants_html(request):
        return error_page(request, status_code)
    return JSONResponse(status_code=status_code, content={"detail": detail})


async def not_authenticated_handler(request: Request, exc: NotAuthenticatedError) -> Response:
    if _wants_html(request):
        next_path = _safe_next_path(request)
        login_url = "/auth/login"
        if next_path and next_path != "/auth/login":
            login_url = f"/auth/login?next={quote(next_path, safe='')}"
        return RedirectResponse(url=login_url, status_code=303)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


async def unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    logger.error(
        "unhandled error on %s %s", request.method, request.url.path, exc_info=exc
    )
    if _wants_html(request):
        try:
            return error_page(request, 500)
        except Exception:  # noqa: BLE001 - never fail while reporting a failure
            pass
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
