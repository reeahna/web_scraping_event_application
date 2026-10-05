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
)
from app.routers.home import _Filters
from app.services import seo
from app.services.rbac import can_access_admin

router = APIRouter(prefix="/events", tags=["public-events"])


# Declared BEFORE the /{event_id} route so "map" is never parsed as an id.
@router.get("/map")
def map_data(request: Request, db: DbSession) -> JSONResponse:
    """Map markers for the current filters — only publicly-visible, matching
    events that have usable coordinates. Carries nothing sensitive (no
    provenance, raw records, configuration, or correction history)."""
    today = current_public_date()
    filters = _Filters(request.query_params, today=today)
    points = list_public_map_points(db, today=today, **filters.as_query_kwargs())
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

    is_saved = False
    is_following_city = False
    if current_user is not None:
        from app.services import engagement

        is_saved = engagement.is_event_saved(db, user_id=current_user.id, event_id=event.id)
        if event.city_id is not None:
            is_following_city = engagement.is_following(
                db, user_id=current_user.id, follow_type="city", target_id=event.city_id
            )

    return render(
        request,
        "public_event_detail.html",
        {
            "current_user": current_user,
            "event": event,
            "can_access_admin": admin_access,
            "latest_run": latest_run,
            "fallback_image_url": get_settings().public_fallback_image_url,
            "is_saved": is_saved,
            "is_following_city": is_following_city,
            "structured_data": seo.event_structured_data(event),
            "meta_description": seo.event_meta_description(event),
            "canonical_url": seo.absolute_url(f"/events/{event.id}"),
        },
    )
