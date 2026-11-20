from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.config import get_settings
from app.core.exceptions import NotFoundError
from app.core.templating import render
from app.dependencies import DbSession, OptionalCurrentUser
from app.models.base import as_aware_utc
from app.repositories.city import get_city
from app.repositories.extraction_run import get_latest_successful_run_for_website
from app.repositories.public_events import (
    get_public_event,
    list_public_map_points,
    local_today,
    town_today,
)
from app.routers.home import _Filters
from app.services import seo
from app.services.event_freshness import event_zone_name, local_timestamp
from app.services.rbac import can_access_admin

router = APIRouter(prefix="/events", tags=["public-events"])


# Declared BEFORE the /{event_id} route so "map" is never parsed as an id.
@router.get("/map")
def map_data(request: Request, db: DbSession) -> JSONResponse:
    """Map markers for the current filters — only publicly-visible, matching
    events that have usable coordinates. Carries nothing sensitive (no
    provenance, raw records, configuration, or correction history)."""
    today = local_today(db)
    filters = _Filters(request.query_params, today=today)
    city = get_city(db, filters.city_id) if filters.city_id is not None else None
    if city is not None:
        # One town's map: its own date, so "today" and "this weekend" match
        # the town page.
        today = town_today(city)
        filters = _Filters(request.query_params, today=today)
    # Like the listing, the map is always one town's: without a town there is
    # nothing to show, rather than every city's events at once.
    if filters.city_id is None:
        return JSONResponse({"points": [], "count": 0})
    points = list_public_map_points(db, today=today, **filters.as_query_kwargs())
    return JSONResponse({"points": points, "count": len(points)})


@router.get("/{event_id}", response_class=HTMLResponse)
def event_detail(event_id: int, request: Request, current_user: OptionalCurrentUser, db: DbSession):
    event = get_public_event(db, event_id, today=local_today(db))
    if event is None:
        raise NotFoundError("Event not found")
    if event.city is not None:
        # For the visitor log: an event page counts toward its town.
        request.state.visit_town = event.city.slug

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
            "content_updated": local_timestamp(
                event.content_updated_at, event_zone_name(event)
            ),
            "content_updated_iso": (
                as_aware_utc(event.content_updated_at).isoformat(timespec="seconds")
                if event.content_updated_at
                else ""
            ),
            "fallback_image_url": get_settings().public_fallback_image_url,
            "is_saved": is_saved,
            "is_following_city": is_following_city,
            "structured_data": seo.event_structured_data(event),
            "breadcrumb_structured_data": seo.breadcrumb_structured_data(
                [("Towns", "/")]
                + ([(event.city.name, seo.town_path(event.city))] if event.city else [])
                + [(event.title, f"/events/{event.id}")]
            ),
            "meta_description": seo.event_meta_description(event),
            "page_title": seo.event_page_title(event),
            "canonical_url": seo.absolute_url(f"/events/{event.id}"),
            "share_links": seo.event_share_links(event),
            "share_text": seo.event_share_text(event),
        },
    )
