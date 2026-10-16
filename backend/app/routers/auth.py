"""The sign-in page and sign-out.

There is no password login: people sign in with Google, Microsoft, Facebook or Yahoo
(app.routers.oauth), so the app never holds a password. This page lists the
providers that are switched on and carries a `next` path through to them.
In development or staging it can also offer "Sign in as the test user"
(TEST_LOGIN_ENABLED, refused in production by the settings).
"""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import get_settings
from app.core.csrf import verify_csrf
from app.core.exceptions import AppError, NotFoundError
from app.core.templating import render
from app.dependencies import ClientIp, CorrelationId, CurrentUser, DbSession
from app.routers.oauth import finish_sign_in
from app.services.audit import record_audit
from app.services.auth import delete_session
from app.services.oauth_login import enabled_providers, safe_next
from app.services.rbac import can_access_admin
from app.services.test_user import ensure_test_user

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str | None = None):
    settings = get_settings()
    return render(
        request,
        "login.html",
        {
            # Only a local path is passed on; anything else is dropped here as
            # well as when the provider sends the person back.
            "next": safe_next(next) if next else "",
            "oauth_providers": enabled_providers(settings),
            "registration_enabled": settings.registration_enabled,
            "test_login_enabled": settings.test_login_enabled,
        },
    )


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
