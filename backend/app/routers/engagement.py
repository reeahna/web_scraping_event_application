"""Saved events, follows, alert preferences, and unsubscribe (Phase 13).

All authenticated routes use CurrentUser and scope every read/write to that
user, so no user can see or change another's data. State-changing posts are
CSRF-protected and redirect (PRG). The unsubscribe route is intentionally
public (token-based) so it works from an email without logging in.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.csrf import verify_csrf
from app.core.exceptions import NotFoundError
from app.core.flash import set_flash
from app.core.templating import render
from app.dependencies import CurrentUser, DbSession, OptionalCurrentUser
from app.models.event import Event
from app.repositories.public_events import current_public_date, get_public_event
from app.services import engagement
from app.services.rbac import can_access_admin

router = APIRouter(tags=["engagement"])

_FOLLOW_TYPES = {"city", "category", "source"}


def _safe_next(value: str | None, default: str) -> str:
    # Only allow local redirects — never an absolute/off-site URL.
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return default


# --- saved events ------------------------------------------------------------


@router.post("/account/save/{event_id}")
def save(
    event_id: int,
    request: Request,
    current_user: CurrentUser,
    db: DbSession,
    csrf_token: str = Form(...),
    next: str = Form(""),
):
    verify_csrf(request, csrf_token)
    # Only allow saving an event the user can actually see.
    if get_public_event(db, event_id, today=current_public_date()) is None:
        raise NotFoundError("Event not found")
    engagement.save_event(db, user_id=current_user.id, event_id=event_id)
    response = RedirectResponse(_safe_next(next, f"/events/{event_id}"), status_code=303)
    set_flash(response, "Saved to your events.")
    return response


@router.post("/account/unsave/{event_id}")
def unsave(
    event_id: int,
    request: Request,
    current_user: CurrentUser,
    db: DbSession,
    csrf_token: str = Form(...),
    next: str = Form(""),
):
    verify_csrf(request, csrf_token)
    engagement.unsave_event(db, user_id=current_user.id, event_id=event_id)
    response = RedirectResponse(_safe_next(next, "/account/saved"), status_code=303)
    set_flash(response, "Removed from your saved events.")
    return response


@router.get("/account/saved", response_class=HTMLResponse)
def saved_page(request: Request, current_user: CurrentUser, db: DbSession):
    events = engagement.list_saved_events(db, user_id=current_user.id)
    return render(
        request,
        "saved_events.html",
        {
            "current_user": current_user,
            "can_access_admin": can_access_admin(db, current_user),
            "events": events,
        },
    )


# --- follows -----------------------------------------------------------------


@router.post("/account/follow")
def follow(
    request: Request,
    current_user: CurrentUser,
    db: DbSession,
    follow_type: str = Form(...),
    target_id: int = Form(...),
    action: str = Form("follow"),
    csrf_token: str = Form(...),
    next: str = Form(""),
):
    verify_csrf(request, csrf_token)
    if follow_type not in _FOLLOW_TYPES:
        raise NotFoundError("Unknown follow target")
    if action == "unfollow":
        engagement.unfollow(
            db, user_id=current_user.id, follow_type=follow_type, target_id=target_id
        )
        message = "Unfollowed."
    else:
        engagement.follow(
            db, user_id=current_user.id, follow_type=follow_type, target_id=target_id
        )
        message = "Following — you'll get alerts for new events."
    response = RedirectResponse(_safe_next(next, "/"), status_code=303)
    set_flash(response, message)
    return response


# --- alert preferences -------------------------------------------------------


@router.get("/account/alerts", response_class=HTMLResponse)
def alert_preferences_page(request: Request, current_user: CurrentUser, db: DbSession):
    prefs = engagement.get_or_create_preferences(db, user_id=current_user.id)
    return render(
        request,
        "alert_preferences.html",
        {
            "current_user": current_user,
            "can_access_admin": can_access_admin(db, current_user),
            "prefs": prefs,
        },
    )


@router.post("/account/alerts", response_class=HTMLResponse)
async def update_alert_preferences(
    request: Request,
    current_user: CurrentUser,
    db: DbSession,
    csrf_token: str = Form(...),
    frequency: str = Form("immediate"),
    in_app_enabled: str = Form(""),
    email_enabled: str = Form(""),
    notify_new_events: str = Form(""),
    notify_reminders: str = Form(""),
    notify_updates: str = Form(""),
):
    verify_csrf(request, csrf_token)
    engagement.update_preferences(
        db,
        user_id=current_user.id,
        values={
            "frequency": frequency,
            "in_app_enabled": in_app_enabled == "1",
            "email_enabled": email_enabled == "1",
            "notify_new_events": notify_new_events == "1",
            "notify_reminders": notify_reminders == "1",
            "notify_updates": notify_updates == "1",
        },
    )
    response = RedirectResponse("/account/alerts", status_code=303)
    set_flash(response, "Alert preferences saved.")
    return response


# --- unsubscribe (public, token-based) --------------------------------------


@router.get("/alerts/unsubscribe", response_class=HTMLResponse)
def unsubscribe_page(
    request: Request, current_user: OptionalCurrentUser, db: DbSession, token: str = ""
):
    prefs = engagement.get_preferences_by_unsubscribe_token(db, token)
    return render(
        request,
        "unsubscribe.html",
        {"current_user": current_user, "token": token, "valid": prefs is not None},
    )


@router.post("/alerts/unsubscribe", response_class=HTMLResponse)
def unsubscribe_confirm(
    request: Request,
    current_user: OptionalCurrentUser,
    db: DbSession,
    token: str = Form(""),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    ok = engagement.unsubscribe_email(db, token)
    response = RedirectResponse(f"/alerts/unsubscribe?token={token}", status_code=303)
    set_flash(
        response,
        "Email alerts turned off." if ok else "That unsubscribe link is not valid.",
    )
    return response


def event_save_state(db, user, event: Event) -> bool:
    """Helper for templates: is this event saved by the current user?"""
    if user is None:
        return False
    return engagement.is_event_saved(db, user_id=user.id, event_id=event.id)
