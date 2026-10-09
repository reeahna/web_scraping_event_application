"""The sign-in page and sign-out.

There is no password login: people sign in with Google, Microsoft or Facebook
(app.routers.oauth), so the app never holds a password. This page lists the
providers that are switched on and carries a `next` path through to them.
"""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import get_settings
from app.core.csrf import verify_csrf
from app.core.templating import render
from app.dependencies import ClientIp, CorrelationId, CurrentUser, DbSession
from app.services.audit import record_audit
from app.services.auth import delete_session
from app.services.oauth_login import enabled_providers, safe_next

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
        },
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
