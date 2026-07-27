"""Legacy comparison admin routes (Phase 16).

A comparison view runs the new-engine preview and reads the legacy events DB
(read-only) for a source, then shows matched / legacy-only / new-only / field
differences / likely duplicates / validation differences. An administrator can
mark a source migrated after review, or it is recorded unavailable if the
legacy source can no longer be read. Never writes to the legacy database and
never starts a scheduler.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.csrf import verify_csrf
from app.core.exceptions import NotFoundError
from app.core.flash import set_flash
from app.core.templating import render
from app.dependencies import DbSession
from app.models.user import User
from app.models.website import Website
from app.services import legacy_comparison
from app.services.rbac import require_permission

router = APIRouter(prefix="/admin/websites", tags=["admin-legacy-comparison"])

ViewSites = Annotated[User, Depends(require_permission("sites.view"))]
ApproveSites = Annotated[User, Depends(require_permission("sites.approve"))]


def _website_or_404(db, website_id: int) -> Website:
    website = db.get(Website, website_id)
    if website is None:
        raise NotFoundError("Website not found")
    return website


@router.get("/{website_id}/legacy-comparison", response_class=HTMLResponse)
async def comparison(
    website_id: int, request: Request, db: DbSession, current_user: ViewSites,
    legacy_source: str = "",
):
    website = _website_or_404(db, website_id)
    source = legacy_source or website.legacy_source_name or (website.source_display_name or "")
    report = None
    error = None
    if source:
        try:
            report = await legacy_comparison.run_comparison(db, website, source)
        except Exception as exc:  # noqa: BLE001 - surface preview/read failures safely
            error = f"Comparison could not run: {type(exc).__name__}"
    return render(
        request,
        "admin/websites/legacy_comparison.html",
        {
            "current_user": current_user,
            "website": website,
            "legacy_source": source,
            "report": report,
            "error": error,
        },
    )


@router.post("/{website_id}/legacy-comparison/status")
def set_status(
    website_id: int, request: Request, db: DbSession, current_user: ApproveSites,
    status: str = Form(...),
    legacy_source: str = Form(""),
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    website = _website_or_404(db, website_id)
    if status not in legacy_comparison.MIGRATION_STATUSES:
        raise NotFoundError("Unknown migration status")
    legacy_comparison.set_migration_status(
        db, website, status, legacy_source=legacy_source or None
    )
    response = RedirectResponse(f"/admin/websites/{website_id}", status_code=303)
    set_flash(response, f"Legacy migration status set to '{status}'.")
    return response
