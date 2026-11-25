"""The sign-in page, sign-in with an emailed code, and sign-out.

There is no password login: people sign in with Google, Microsoft, Facebook, X or Yahoo
(app.routers.oauth) or with a 6-digit code sent to their email
(app.services.email_sign_in), so the app never holds a password. This page lists
the ways that are switched on and carries a `next` path through to them.
In development or staging it can also offer "Sign in as the test user"
(TEST_LOGIN_ENABLED, refused in production by the settings).
"""

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import get_settings
from app.core.csrf import verify_csrf
from app.core.exceptions import AppError, NotFoundError
from app.core.templating import render
from app.dependencies import ClientIp, CorrelationId, CurrentUser, DbSession
from app.routers.oauth import finish_sign_in
from app.services import email_sign_in
from app.services.audit import record_audit
from app.services.auth import delete_session
from app.services.email import email_sign_in_available, get_sign_in_email_sender
from app.services.oauth_login import enabled_providers, safe_next
from app.services.rbac import can_access_admin
from app.services.test_user import ensure_test_user

router = APIRouter(prefix="/auth", tags=["auth"])


# Remembers which address the code went to, between sending it and typing it
# in, so the address is not put in the URL (and so in logs and history).
EMAIL_COOKIE = "email_sign_in_address"


def _render_login(
    request: Request, next_url: str, *, email: str = "", error: str | None = None,
    status_code: int = 200,
):
    settings = get_settings()
    return render(
        request,
        "login.html",
        {
            # Only a local path is passed on; anything else is dropped here as
            # well as when the provider sends the person back.
            "next": next_url,
            "oauth_providers": enabled_providers(settings),
            "email_sign_in_enabled": email_sign_in_available(settings),
            "email": email,
            "email_error": error,
            "registration_enabled": settings.registration_enabled,
            "test_login_enabled": settings.test_login_enabled,
        },
        status_code=status_code,
    )


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str | None = None):
    return _render_login(request, safe_next(next) if next else "")


def _require_email_sign_in():
    if not email_sign_in_available(get_settings()):
        raise NotFoundError("Not found.")


def _render_code_page(
    request: Request, email: str, next_url: str, *, error: str | None = None,
    status_code: int = 200,
):
    return render(
        request,
        "login_code.html",
        {
            "email": email,
            "next": next_url,
            "error": error,
            "code_length": email_sign_in.CODE_LENGTH,
            "code_minutes": int(email_sign_in.CODE_TTL.total_seconds() // 60),
        },
        status_code=status_code,
    )


@router.post("/email/request", response_class=HTMLResponse)
def request_email_code(
    request: Request,
    db: DbSession,
    ip_address: ClientIp,
    csrf_token: str = Form(...),
    email: str = Form(""),
    next: str = Form(""),
):
    _require_email_sign_in()
    verify_csrf(request, csrf_token)
    settings = get_settings()
    next_url = safe_next(next) if next else ""
    try:
        result = email_sign_in.request_code(
            db, settings, get_sign_in_email_sender(settings), email[:320],
            ip_address=ip_address, user_agent=request.headers.get("user-agent"),
        )
    except email_sign_in.EmailSignInError as exc:
        return _render_login(
            request, next_url, email=email[:320], error=exc.message,
            status_code=exc.status_code,
        )
    target = "/auth/email/verify" + (f"?next={quote(next_url)}" if next_url else "")
    response = RedirectResponse(url=target, status_code=303)
    response.set_cookie(
        EMAIL_COOKIE,
        result.email,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/auth",
        max_age=int(email_sign_in.CODE_TTL.total_seconds()),
    )
    return response


@router.get("/email/verify", response_class=HTMLResponse)
def email_code_page(request: Request, next: str | None = None):
    _require_email_sign_in()
    next_url = safe_next(next) if next else ""
    email = request.cookies.get(EMAIL_COOKIE, "")
    try:
        email = email_sign_in.clean_email(email)
    except email_sign_in.EmailSignInError:
        # Nothing sent from this browser lately: start again.
        target = "/auth/login" + (f"?next={quote(next_url)}" if next_url else "")
        return RedirectResponse(url=target, status_code=303)
    return _render_code_page(request, email, next_url)


@router.post("/email/verify", response_class=HTMLResponse)
def verify_email_code(
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    csrf_token: str = Form(...),
    code: str = Form(""),
    next: str = Form(""),
):
    _require_email_sign_in()
    verify_csrf(request, csrf_token)
    settings = get_settings()
    next_url = safe_next(next) if next else ""
    email = request.cookies.get(EMAIL_COOKIE, "")
    try:
        user = email_sign_in.verify_code(
            db, settings, email, code[:32],
            ip_address=ip_address, user_agent=request.headers.get("user-agent"),
        )
    except email_sign_in.EmailSignInError as exc:
        if exc.code == "invalid_email":
            target = "/auth/login" + (f"?next={quote(next_url)}" if next_url else "")
            return RedirectResponse(url=target, status_code=303)
        return _render_code_page(
            request, email_sign_in.clean_email(email), next_url, error=exc.message,
            status_code=exc.status_code,
        )
    response = finish_sign_in(
        db, request, user, safe_next(next_url),
        detail="Email code sign-in",
        correlation_id=correlation_id,
        ip_address=ip_address,
        method=email_sign_in.METHOD,
    )
    response.delete_cookie(EMAIL_COOKIE, path="/auth")
    return response


@router.post("/test-login")
def test_login(
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    csrf_token: str = Form(...),
    next: str = Form(""),
):
    settings = get_settings()
    if not settings.test_login_enabled:
        raise NotFoundError("Not found.")
    verify_csrf(request, csrf_token)
    user = ensure_test_user(db, settings.test_user_email)
    if can_access_admin(db, user):
        # A test sign-in needs no credentials, so it must never open an admin
        # account (say TEST_USER_EMAIL was set to an administrator's address).
        raise AppError(
            "TEST_USER_EMAIL belongs to an administrator; use an ordinary account.",
            status_code=403,
        )
    return finish_sign_in(
        db, request, user, safe_next(next),
        detail="Test user sign-in",
        correlation_id=correlation_id,
        ip_address=ip_address,
        method="test",
    )


@router.post("/logout")
def logout(
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    settings = get_settings()

    token = request.cookies.get(settings.session_cookie_name)
    if token:
        delete_session(db, token)

    record_audit(
        db,
        actor_id=current_user.id,
        action="logout",
        entity_type="user",
        entity_id=current_user.id,
        correlation_id=correlation_id,
        ip_address=ip_address,
    )

    response = RedirectResponse(url="/auth/login", status_code=303)
    response.delete_cookie(settings.session_cookie_name, path="/")
    return response
