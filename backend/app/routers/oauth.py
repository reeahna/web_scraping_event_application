"""External-authentication routes (Phase 14).

`GET /auth/oauth/{provider}` starts the flow (redirect to the provider);
`GET /auth/oauth/{provider}/callback` completes it. A disabled provider 404s,
so the app runs with any or all providers off. On success we mint a brand-new
session (session-fixation prevention) and audit the login; a failure is audited
and returns the user to the login page with a safe message.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app.config import get_settings
from app.core.exceptions import AppError, NotFoundError
from app.core.flash import set_flash
from app.dependencies import ClientIp, CorrelationId, DbSession
from app.services import oauth_login
from app.services.audit import record_audit
from app.services.auth import create_session
from app.services.rate_limit import sign_in_start_allowed
from app.services.rbac import can_access_admin

router = APIRouter(prefix="/auth/oauth", tags=["oauth"])


@router.get("/{provider}")
def start(provider: str, request: Request, db: DbSession, ip_address: ClientIp, next: str = ""):
    settings = get_settings()
    if not oauth_login.is_enabled(settings, provider):
        raise NotFoundError("That sign-in provider is not available.")
    if not sign_in_start_allowed(ip_address):
        raise AppError("Too many sign-in attempts. Please wait a few minutes.", status_code=429)
    authorize_url = oauth_login.start_login(db, settings, provider, next_url=next)
    return RedirectResponse(authorize_url, status_code=303)


@router.get("/{provider}/callback")
def callback(
    provider: str,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    code: str = "",
    state: str = "",
    error: str = "",
):
    settings = get_settings()
    if not oauth_login.is_enabled(settings, provider):
        raise NotFoundError("That sign-in provider is not available.")

    if error or not code or not state:
        return _fail(db, provider, correlation_id, ip_address, reason=error or "missing_code")

    try:
        user, next_url = oauth_login.complete_login(
            db, settings, provider, code=code, state=state
        )
    except oauth_login.OAuthError as exc:
        return _fail(db, provider, correlation_id, ip_address, reason=exc.code)

    raw_token = create_session(db, user, request)
    user.last_login_at = datetime.now(UTC)
    db.commit()
    record_audit(
        db,
        actor_id=user.id,
        action="login",
        entity_type="user",
        entity_id=user.id,
        detail=f"OAuth login via {provider}",
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    if next_url == "/":
        # No page to return to: admins land in the admin, everyone else on
        # their account page.
        next_url = "/admin" if can_access_admin(db, user) else "/account"
    response = RedirectResponse(url=next_url, status_code=303)
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


# What a person is told when sign-in is refused for a known reason.
_FAILURE_MESSAGES = {
    "account_disabled": "This account has been disabled.",
    "registration_closed": "New accounts aren't being accepted right now.",
    "email_required": "That account didn't share an email address, which is needed to sign in.",
    "email_unverified_conflict": (
        "That email already has an account here. Sign in with a provider that has "
        "verified the address, such as Google."
    ),
}


def _fail(db, provider: str, correlation_id, ip_address, *, reason: str) -> RedirectResponse:
    record_audit(
        db,
        actor_id=None,
        action="login_failed",
        entity_type="user",
        detail=f"OAuth login via {provider} failed: {reason}",
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    message = _FAILURE_MESSAGES.get(reason, "Sign-in did not complete. Please try again.")
    response = RedirectResponse(url="/auth/login", status_code=303)
    set_flash(response, message, category="error")
    return response
