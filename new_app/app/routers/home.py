from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.core.templating import render
from app.dependencies import DbSession, OptionalCurrentUser
from app.repositories.city import list_cities
from app.repositories.event_category import list_active_categories
from app.repositories.public_events import current_public_date, list_public_events
from app.services.rbac import can_access_admin

router = APIRouter()


def _parse_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


@router.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    current_user: OptionalCurrentUser,
    db: DbSession,
    city_id: str | None = None,
    category_id: str | None = None,
    upcoming_only: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: str | None = None,
):
    settings = get_settings()

    parsed_city_id = _parse_int(city_id)
    parsed_category_id = _parse_int(category_id)
    parsed_upcoming_only = upcoming_only == "1"
    parsed_date_from = _parse_date(date_from)
    parsed_date_to = _parse_date(date_to)
    page_number = max(_parse_int(page) or 1, 1)

    events, total, has_next = list_public_events(
        db,
        today=current_public_date(),
        city_id=parsed_city_id,
        category_id=parsed_category_id,
        upcoming_only=parsed_upcoming_only,
        date_from=parsed_date_from,
        date_to=parsed_date_to,
        page=page_number,
    )

    cities = list_cities(db)
    selected_city = next((c for c in cities if c.id == parsed_city_id), None)

    return render(
        request,
        "home.html",
        {
            "current_user": current_user,
            "can_access_admin": can_access_admin(db, current_user) if current_user else False,
            "registration_enabled": settings.registration_enabled,
            "events": events,
            "total": total,
            "page": page_number,
            "has_next": has_next,
            "cities": cities,
            "selected_city": selected_city,
            "categories": list_active_categories(db),
            "filters": {
                "city_id": parsed_city_id,
                "category_id": parsed_category_id,
                "upcoming_only": parsed_upcoming_only,
                "date_from": date_from or "",
                "date_to": date_to or "",
            },
        },
    )
