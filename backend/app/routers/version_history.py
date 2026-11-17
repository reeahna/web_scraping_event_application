"""Version history: the public What's new page and the admin version history.

Both are built from CHANGELOG.md (see app.services.changelog). The public page
shows only the notes written for everyone; the admin page shows every note,
links PR numbers to GitHub, and includes what is waiting in Unreleased.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.core.exceptions import AppError
from app.core.templating import render
from app.dependencies import CurrentUser, DbSession, OptionalCurrentUser
from app.services.changelog import (
    APP_VERSION,
    load_changelog,
    public_releases,
)
from app.services.rbac import can_access_admin

router = APIRouter(tags=["version history"])


@router.get("/whats-new", response_class=HTMLResponse)
def whats_new(request: Request, current_user: OptionalCurrentUser, db: DbSession):
    return render(
        request,
        "whats_new.html",
        {
            "current_user": current_user,
            "can_access_admin": can_access_admin(db, current_user) if current_user else False,
            "releases": public_releases(),
            "version": APP_VERSION,
        },
    )


@router.get("/admin/version-history", response_class=HTMLResponse)
def admin_version_history(request: Request, current_user: CurrentUser, db: DbSession):
    if not can_access_admin(db, current_user):
        raise AppError("Forbidden: no admin access", status_code=403)
    releases = list(load_changelog())
    unreleased = next((r for r in releases if r.is_unreleased), None)
    return render(
        request,
        "admin/version_history.html",
        {
            "current_user": current_user,
            "unreleased": unreleased if unreleased and unreleased.has_notes else None,
            "releases": [r for r in releases if not r.is_unreleased],
            "version": APP_VERSION,
        },
    )
