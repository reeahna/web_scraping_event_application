from urllib.parse import quote

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response


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


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


async def not_authenticated_handler(request: Request, exc: NotAuthenticatedError) -> Response:
    if _wants_html(request):
        next_path = _safe_next_path(request)
        login_url = "/auth/login"
        if next_path and next_path != "/auth/login":
            login_url = f"/auth/login?next={quote(next_path, safe='')}"
        return RedirectResponse(url=login_url, status_code=303)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
