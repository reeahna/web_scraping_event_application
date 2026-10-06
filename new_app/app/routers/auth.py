import re
from datetime import UTC, datetime
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.csrf import verify_csrf
from app.core.email import normalize_email
from app.core.flash import set_flash
from app.core.templating import render
from app.dependencies import ClientIp, CorrelationId, CurrentUser, DbSession
from app.models.user import User
from app.models.user_token import EMAIL_VERIFICATION, PASSWORD_RESET
from app.schemas.registration import NewPassword
from app.services.account_email import (
    find_valid_token,
    request_password_reset,
    reset_password,
    verify_email,
)
from app.services.audit import record_audit
from app.services.auth import authenticate_local_user, create_session, delete_session
from app.services.email import email_delivery_available
from app.services.rate_limit import (
    clear_login_failures,
    get_rate_limit_backend,
    login_is_locked,
    record_login_failure,
)
from app.services.rbac import can_access_admin

router = APIRouter(prefix="/auth", tags=["auth"])


def _is_safe_next(next_url: str | None) -> bool:
    """Only same-app relative paths — never an absolute URL, an external
    scheme, or a protocol-relative `//host` path, to avoid open-redirect via
    a crafted `next` value."""
    if not next_url or any(ord(char) < 32 or ord(char) == 127 for char in next_url):
        return False
    if not next_url.startswith("/") or next_url.startswith("//"):
        return False
    if "\\" in next_url or re.search(r"%(?![0-9A-Fa-f]{2})", next_url):
        return False
    try:
        parsed = urlsplit(next_url)
        decoded_path = unquote(parsed.path)
    except (UnicodeError, ValueError):
        return False
    return (
        not parsed.scheme
        and not parsed.netloc
        and not parsed.fragment
        and decoded_path.startswith("/")
        and not decoded_path.startswith("//")
        and "\\" not in decoded_path
    )


def _default_redirect(db: Session, user: User) -> str:
    """Where a user lands after a *normal* login with no `next`: admins land
    on /admin, everyone else on /account. Based on effective permissions
    (can_access_admin), not role name, so a custom low/high-permission role
    behaves correctly too."""
    return "/admin" if can_access_admin(db, user) else "/account"


def _resolve_redirect(db: Session, user: User, next_url: str | None) -> str:
    if _is_safe_next(next_url):
        return next_url
    return _default_redirect(db, user)


def _render_login(
    request: Request,
    *,
    error: str | None = None,
    status_code: int = 200,
    next_url: str | None = None,
) -> HTMLResponse:
    settings = get_settings()
    from app.services.oauth_login import enabled_providers

    return render(
        request,
        "login.html",
        {
            "error": error,
            "local_login_enabled": settings.local_login_enabled,
            "registration_enabled": settings.registration_enabled,
            "next": next_url or "",
            "oauth_providers": enabled_providers(settings),
            # The "development login" warning was shown on the live site too.
            "is_development": settings.app_env == "development",
            "password_reset_available": email_delivery_available(settings),
        },
        status_code=status_code,
    )


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str | None = None):
    return _render_login(request, next_url=next)


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    email: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
    next: str = Form(""),
):
    settings = get_settings()
    verify_csrf(request, csrf_token)

    if not settings.local_login_enabled:
        return _render_login(
            request, error="Local password login is disabled.", status_code=404, next_url=next
        )

    normalized = normalize_email(email)
    if login_is_locked(normalized, ip_address):
        record_audit(
            db,
            actor_id=None,
            action="login_locked_out",
            entity_type="user",
            detail="Too many failed login attempts",
            correlation_id=correlation_id,
            ip_address=ip_address,
        )
        return _render_login(
            request,
            error=(
                "Too many failed attempts. Please wait 15 minutes and try again"
                + (", or reset your password." if email_delivery_available() else ".")
            ),
            status_code=429,
            next_url=next,
        )

    user = authenticate_local_user(db, email, password)
    if user is None:
        record_login_failure(normalized, ip_address)
        record_audit(
            db,
            actor_id=None,
            action="login_failed",
            entity_type="user",
            detail=f"Failed login attempt for email={email}",
            correlation_id=correlation_id,
            ip_address=ip_address,
        )
        return _render_login(
            request, error="Invalid email or password.", status_code=401, next_url=next
        )

    if not user.is_active:
        record_audit(
            db,
            actor_id=user.id,
            action="login_failed",
            entity_type="user",
            entity_id=user.id,
            detail="Account is disabled",
            correlation_id=correlation_id,
            ip_address=ip_address,
        )
        return _render_login(
            request, error="This account is disabled.", status_code=403, next_url=next
        )

    clear_login_failures(normalized)
    raw_token = create_session(db, user, request)
    user.last_login_at = datetime.now(UTC)
    db.commit()

    record_audit(
        db,
        actor_id=user.id,
        action="login",
        entity_type="user",
        entity_id=user.id,
        correlation_id=correlation_id,
        ip_address=ip_address,
    )

    response = RedirectResponse(url=_resolve_redirect(db, user, next), status_code=303)
    response.set_cookie(
        settings.session_cookie_name,
        raw_token,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
        path="/",
        max_age=settings.session_ttl_seconds,
    )
    return response


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


# --- forgot / reset password --------------------------------------------------

# Requests per hour before reset emails stop going out. Generous for a person
# who mistyped, tight enough that the form cannot be used to flood an inbox.
_RESET_REQUESTS_PER_IP = 10
_RESET_REQUESTS_PER_ADDRESS = 3


def _render_auth_page(request: Request, template: str, status_code: int = 200, **context):
    return render(
        request,
        template,
        {"email_available": email_delivery_available(), **context},
        status_code=status_code,
    )


@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_form(request: Request):
    return _render_auth_page(request, "forgot_password.html")


@router.post("/forgot-password", response_class=HTMLResponse)
def forgot_password_submit(
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    email: str = Form(...),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    if not email_delivery_available():
        return _render_auth_page(request, "forgot_password.html", status_code=503)

    normalized = normalize_email(email)
    backend = get_rate_limit_backend()
    within_limits = backend.allow(
        f"reset-ip:{ip_address}", limit=_RESET_REQUESTS_PER_IP, window_seconds=3600
    ) and backend.allow(
        f"reset-address:{normalized}", limit=_RESET_REQUESTS_PER_ADDRESS, window_seconds=3600
    )
    if within_limits:
        request_password_reset(db, normalized)
        record_audit(
            db,
            actor_id=None,
            action="password_reset_requested",
            entity_type="user",
            correlation_id=correlation_id,
            ip_address=ip_address,
        )
    # The same answer whether or not the address has an account (or was
    # throttled), so the form reveals nothing about who is registered.
    return _render_auth_page(request, "forgot_password.html", sent_to=normalized)


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_form(request: Request, db: DbSession, token: str = ""):
    valid = find_valid_token(db, token, PASSWORD_RESET) is not None
    return _render_auth_page(
        request, "reset_password.html", status_code=200 if valid else 400,
        token=token, token_valid=valid, errors={},
    )


@router.post("/reset-password", response_class=HTMLResponse)
def reset_password_submit(
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    token: str = Form(""),
    password: str = Form(""),
    password_confirm: str = Form(""),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    found = find_valid_token(db, token, PASSWORD_RESET)
    if found is None:
        return _render_auth_page(
            request, "reset_password.html", status_code=400,
            token=token, token_valid=False, errors={},
        )
    try:
        new = NewPassword(password=password, password_confirm=password_confirm)
    except ValidationError as exc:
        errors = {str(e["loc"][0]): e["msg"].removeprefix("Value error, ") for e in exc.errors()}
        return _render_auth_page(
            request, "reset_password.html", status_code=422,
            token=token, token_valid=True, errors=errors,
        )

    user = reset_password(db, found, new.password)
    clear_login_failures(user.email)
    record_audit(
        db,
        actor_id=user.id,
        action="password_reset",
        entity_type="user",
        entity_id=user.id,
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse(url="/auth/login", status_code=303)
    response.delete_cookie(get_settings().session_cookie_name, path="/")
    set_flash(response, "Your password has been changed. Please log in with the new one.")
    return response


# --- email verification ---------------------------------------------------------


@router.get("/verify-email", response_class=HTMLResponse)
def verify_email_link(
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    token: str = "",
):
    found = find_valid_token(db, token, EMAIL_VERIFICATION)
    if found is None:
        return _render_auth_page(request, "verify_email_failed.html", status_code=400)
    user = verify_email(db, found)
    record_audit(
        db,
        actor_id=user.id,
        action="email_verified",
        entity_type="user",
        entity_id=user.id,
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse(url="/account", status_code=303)
    set_flash(response, "Thanks, your email address is confirmed.")
    return response
