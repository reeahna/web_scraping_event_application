"""Static information pages: the privacy policy and the terms of use."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.core.templating import render
from app.dependencies import DbSession, OptionalCurrentUser
from app.services.rbac import can_access_admin

router = APIRouter(tags=["pages"])

# Shown on both pages. Change it whenever either page's substance changes.
POLICIES_UPDATED = "October 9, 2026"


def _render_page(request: Request, template: str, current_user, db) -> HTMLResponse:
    settings = get_settings()
    return render(
        request,
        template,
        {
            "current_user": current_user,
            "can_access_admin": can_access_admin(db, current_user) if current_user else False,
            "registration_enabled": settings.registration_enabled,
            "contact_email": settings.contact_email,
            "updated": POLICIES_UPDATED,
        },
    )


@router.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request, current_user: OptionalCurrentUser, db: DbSession):
    return _render_page(request, "privacy.html", current_user, db)


@router.get("/terms", response_class=HTMLResponse)
def terms(request: Request, current_user: OptionalCurrentUser, db: DbSession):
    return _render_page(request, "terms.html", current_user, db)
