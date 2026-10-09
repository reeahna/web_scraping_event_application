"""Admin geocoding controls (Phase 11): status view and manual retry.

Retry only requeues an event (sets it back to ``pending``); the dedicated
scheduler process performs the actual geocoding on its next drain. Nothing here
calls a geocoding provider inline in the web request.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from app.core.csrf import verify_csrf
from app.core.exceptions import NotFoundError
from app.core.templating import render
from app.database import get_db
from app.models.event import Event
from app.models.user import User
from app.repositories.geocoding import geocoding_overview
from app.services.geocoding import retry_event_geocoding
from app.services.rbac import require_permission

router = APIRouter(prefix="/admin/geocoding", tags=["admin-geocoding"])

ViewSites = Annotated[User, Depends(require_permission("sites.view"))]
UpdateSites = Annotated[User, Depends(require_permission("sites.update"))]
DbSession = Annotated[Session, Depends(get_db)]


def _verify_csrf(request: Request) -> None:
    verify_csrf(request, request.headers.get("X-CSRF-Token"))


CsrfChecked = Annotated[None, Depends(_verify_csrf)]


@router.get("", response_class=HTMLResponse)
def overview_page(request: Request, current_user: ViewSites, db: DbSession):
    """The status below as a readable page."""
    return render(
        request,
        "admin/geocoding.html",
        {"current_user": current_user, "overview": geocoding_overview(db)},
    )


@router.get("/status")
def status(db: DbSession, _: ViewSites) -> JSONResponse:
    """Geocoding progress as JSON (see `geocoding_overview`)."""
    return JSONResponse(geocoding_overview(db))


@router.post("/events/{event_id}/retry")
def retry(event_id: int, db: DbSession, _: UpdateSites, __: CsrfChecked) -> JSONResponse:
    event = db.get(Event, event_id)
    if event is None:
        raise NotFoundError("Event not found")
    retry_event_geocoding(db, event)
    return JSONResponse({"geocode_status": event.geocode_status})
