from fastapi import Request, Response

from app.config import get_settings
from app.core.exceptions import AppError
from app.core.security import csrf_tokens_match, generate_csrf_token


def get_or_create_csrf_token(request: Request) -> tuple[str, bool]:
    """Return (token, is_new). If is_new, the caller must set it as a cookie
    on the response it returns."""
    settings = get_settings()
    token = request.cookies.get(settings.csrf_cookie_name)
    if token:
        return token, False
    return generate_csrf_token(), True


def set_csrf_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        settings.csrf_cookie_name,
        token,
        httponly=False,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
    )


def verify_csrf(request: Request, submitted_token: str | None) -> None:
    settings = get_settings()
    cookie_token = request.cookies.get(settings.csrf_cookie_name)
    if not csrf_tokens_match(cookie_token, submitted_token):
        raise AppError("Invalid or missing CSRF token", status_code=403)
