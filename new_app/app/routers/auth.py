from datetime import UTC, datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import get_settings
from app.core.csrf import verify_csrf
from app.core.templating import render
from app.dependencies import ClientIp, CorrelationId, CurrentUser, DbSession
from app.services.audit import record_audit
from app.services.auth import authenticate_local_user, create_session, delete_session

router = APIRouter(prefix="/auth", tags=["auth"])


def _safe_next(next_url: str | None) -> str:
    """Only ever redirect to a same-app relative path — never an absolute or
    protocol-relative URL, to avoid open-redirect via a crafted `next` value."""
    if not next_url:
        return "/admin"
    if next_url.startswith("/") and not next_url.startswith(("//", "/\\")):
        return next_url
    return "/admin"


def _render_login(
    request: Request,
    *,
    error: str | None = None,
    status_code: int = 200,
    next_url: str | None = None,
) -> HTMLResponse:
    settings = get_settings()
    return render(
        request,
        "login.html",
        {
            "error": error,
            "local_login_enabled": settings.local_login_enabled,
            "next": next_url or "",
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

    user = authenticate_local_user(db, email, password)
    if user is None:
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

    response = RedirectResponse(url=_safe_next(next), status_code=303)
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
