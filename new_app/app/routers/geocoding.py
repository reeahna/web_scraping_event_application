"""Admin geocoding controls (Phase 11): status view and manual retry.

Retry only requeues an event (sets it back to ``pending``); the dedicated
scheduler process performs the actual geocoding on its next drain. Nothing here
calls a geocoding provider inline in the web request.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.csrf import verify_csrf
from app.core.exceptions import NotFoundError
from app.database import get_db
from app.models.event import Event
from app.models.user import User
from app.services.geocoding import retry_event_geocoding
from app.services.rbac import require_permission

router = APIRouter(prefix="/admin/geocoding", tags=["admin-geocoding"])

ViewSites = Annotated[User, Depends(require_permission("sites.view"))]
UpdateSites = Annotated[User, Depends(require_permission("sites.update"))]
DbSession = Annotated[Session, Depends(get_db)]


def _verify_csrf(request: Request) -> None:
    verify_csrf(request, request.headers.get("X-CSRF-Token"))


CsrfChecked = Annotated[None, Depends(_verify_csrf)]


@router.get("/status")
def status(db: DbSession, _: ViewSites) -> JSONResponse:
    rows = db.execute(
        select(Event.geocode_status, func.count()).group_by(Event.geocode_status)
    ).all()
    return JSONResponse({"counts": {status_value: count for status_value, count in rows}})


@router.post("/events/{event_id}/retry")
def retry(event_id: int, db: DbSession, _: UpdateSites, __: CsrfChecked) -> JSONResponse:
    event = db.get(Event, event_id)
    if event is None:
        raise NotFoundError("Event not found")
    retry_event_geocoding(db, event)
    return JSONResponse({"geocode_status": event.geocode_status})
