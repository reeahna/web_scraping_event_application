from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.config import get_settings
from app.core.exceptions import NotFoundError
from app.core.templating import render
from app.dependencies import DbSession, OptionalCurrentUser
from app.repositories.extraction_run import get_latest_successful_run_for_website
from app.repositories.public_events import (
    current_public_date,
    get_public_event,
    list_public_map_points,
    this_weekend,
)
from app.services.rbac import can_access_admin

router = APIRouter(prefix="/events", tags=["public-events"])


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value else None
    except ValueError:
        return None


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


# Declared BEFORE the /{event_id} route so "map" is never parsed as an id.
@router.get("/map")
def map_data(request: Request, db: DbSession) -> JSONResponse:
    """Map markers for the current filters — only publicly-visible, matching
    events that have usable coordinates. Carries nothing sensitive (no
    provenance, raw records, configuration, or correction history)."""
    params = request.query_params
    today = current_public_date()
    preset = params.get("preset")
    if preset == "today":
        date_from = date_to = today
    elif preset == "weekend":
        date_from, date_to = this_weekend(today)
    else:
        date_from, date_to = _date(params.get("date_from")), _date(params.get("date_to"))
    recurrence = params.get("recurrence")
    points = list_public_map_points(
        db,
        today=today,
        city_id=_int(params.get("city_id")),
        category_id=_int(params.get("category_id")),
        source_id=_int(params.get("source_id")),
        search=(params.get("q") or "").strip() or None,
        recurrence=recurrence if recurrence in ("single", "recurring") else None,
        upcoming_only=params.get("upcoming_only") == "1",
        date_from=date_from,
        date_to=date_to,
    )
    return JSONResponse({"points": points, "count": len(points)})


@router.get("/{event_id}", response_class=HTMLResponse)
def event_detail(event_id: int, request: Request, current_user: OptionalCurrentUser, db: DbSession):
    event = get_public_event(db, event_id, today=current_public_date())
    if event is None:
        raise NotFoundError("Event not found")

    admin_access = can_access_admin(db, current_user) if current_user else False
    latest_run = None
    if admin_access and event.website_id is not None:
        latest_run = get_latest_successful_run_for_website(db, event.website_id)

    return render(
        request,
        "public_event_detail.html",
        {
            "current_user": current_user,
            "event": event,
            "can_access_admin": admin_access,
            "latest_run": latest_run,
            "fallback_image_url": get_settings().public_fallback_image_url,
        },
    )
