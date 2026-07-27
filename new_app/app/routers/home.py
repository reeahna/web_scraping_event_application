from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.core.exceptions import NotFoundError
from app.core.templating import render
from app.dependencies import DbSession, OptionalCurrentUser
from app.repositories.city import get_city_by_slug, list_cities
from app.repositories.event_category import list_active_categories
from app.repositories.public_events import (
    current_public_date,
    list_public_events,
    list_public_sources,
    this_weekend,
)
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


class _Filters:
    """Parsed, validated public filters plus the date range a preset expands
    to. Kept in one place so the list route, the city route, and the map
    endpoint all interpret the query string identically."""

    def __init__(self, params: dict[str, str | None], *, today: date):
        self.city_id = _parse_int(params.get("city_id"))
        self.category_id = _parse_int(params.get("category_id"))
        self.source_id = _parse_int(params.get("source_id"))
        self.search = (params.get("q") or "").strip() or None
        recurrence = params.get("recurrence")
        self.recurrence = recurrence if recurrence in ("single", "recurring") else None
        self.upcoming_only = params.get("upcoming_only") == "1"
        self.view = "map" if params.get("view") == "map" else "list"
        self.preset = params.get("preset") if params.get("preset") in ("today", "weekend") else None

        # A preset overrides explicit from/to so a shareable ?preset=today URL
        # is self-contained.
        if self.preset == "today":
            self.date_from = self.date_to = today
        elif self.preset == "weekend":
            self.date_from, self.date_to = this_weekend(today)
        else:
            self.date_from = _parse_date(params.get("date_from"))
            self.date_to = _parse_date(params.get("date_to"))

    def as_query_kwargs(self) -> dict:
        return {
            "city_id": self.city_id,
            "category_id": self.category_id,
            "source_id": self.source_id,
            "search": self.search,
            "recurrence": self.recurrence,
            "upcoming_only": self.upcoming_only,
            "date_from": self.date_from,
            "date_to": self.date_to,
        }

    def query_string(self) -> str:
        """The current filters as a URL query string (without page/view), so
        pagination, the view toggle, and the map fetch all stay shareable and
        in sync. A preset is emitted as ?preset=... rather than its expanded
        dates, keeping the URL tidy."""
        pairs: list[tuple[str, str]] = []
        if self.city_id:
            pairs.append(("city_id", str(self.city_id)))
        if self.category_id:
            pairs.append(("category_id", str(self.category_id)))
        if self.source_id:
            pairs.append(("source_id", str(self.source_id)))
        if self.search:
            pairs.append(("q", self.search))
        if self.recurrence:
            pairs.append(("recurrence", self.recurrence))
        if self.upcoming_only:
            pairs.append(("upcoming_only", "1"))
        if self.preset:
            pairs.append(("preset", self.preset))
        else:
            if self.date_from:
                pairs.append(("date_from", self.date_from.isoformat()))
            if self.date_to:
                pairs.append(("date_to", self.date_to.isoformat()))
        return urlencode(pairs)

    def template_context(self) -> dict:
        return {
            "city_id": self.city_id,
            "category_id": self.category_id,
            "source_id": self.source_id,
            "q": self.search or "",
            "recurrence": self.recurrence or "",
            "upcoming_only": self.upcoming_only,
            "date_from": self.date_from.isoformat() if self.date_from else "",
            "date_to": self.date_to.isoformat() if self.date_to else "",
            "preset": self.preset or "",
            "view": self.view,
        }


def _page_number(request: Request) -> int:
    return max(_parse_int(request.query_params.get("page")) or 1, 1)


def _render_events(
    request: Request,
    db,
    current_user,
    filters: _Filters,
    *,
    selected_city=None,
    base_path: str = "/",
):
    settings = get_settings()
    today = current_public_date()
    page = _page_number(request)
    events, total, has_next = list_public_events(
        db, today=today, page=page, **filters.as_query_kwargs()
    )
    return render(
        request,
        "home.html",
        {
            "current_user": current_user,
            "can_access_admin": can_access_admin(db, current_user) if current_user else False,
            "registration_enabled": settings.registration_enabled,
            "events": events,
            "total": total,
            "page": page,
            "has_next": has_next,
            "cities": list_cities(db),
            "selected_city": selected_city,
            "categories": list_active_categories(db),
            "sources": list_public_sources(db, today=today, city_id=filters.city_id),
            "filters": filters.template_context(),
            "query_string": filters.query_string(),
            "base_path": base_path,
            "fallback_image_url": settings.public_fallback_image_url,
            "map_tile_url": settings.public_map_tile_url,
            "map_attribution": settings.public_map_attribution,
        },
    )


@router.get("/", response_class=HTMLResponse)
def home(request: Request, current_user: OptionalCurrentUser, db: DbSession):
    filters = _Filters(dict(request.query_params), today=current_public_date())
    return _render_events(request, db, current_user, filters, base_path="/")


@router.get("/city/{slug}", response_class=HTMLResponse)
def city_page(slug: str, request: Request, current_user: OptionalCurrentUser, db: DbSession):
    city = get_city_by_slug(db, slug)
    if city is None or not city.is_active:
        raise NotFoundError("City not found")
    params = dict(request.query_params)
    params["city_id"] = str(city.id)  # the city page pins the city filter
    filters = _Filters(params, today=current_public_date())
    return _render_events(
        request, db, current_user, filters, selected_city=city, base_path=f"/city/{slug}"
    )
