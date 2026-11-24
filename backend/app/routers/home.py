from datetime import date
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.datastructures import QueryParams

from app.config import get_settings
from app.core.exceptions import NotFoundError
from app.core.templating import render
from app.dependencies import DbSession, OptionalCurrentUser
from app.repositories.city import (
    get_city_by_slug,
    get_city_by_slug_ignoring_case,
    list_cities,
    search_public_cities,
)
from app.repositories.event_category import list_active_categories
from app.repositories.public_events import (
    PUBLIC_EVENTS_PER_PAGE,
    list_public_events,
    list_public_sources,
    local_today,
    this_weekend,
    town_today,
    upcoming_counts_by_city,
)
from app.services import seo
from app.services.event_freshness import local_timestamp, town_listings_checked
from app.services.rbac import can_access_admin, user_has_permission

router = APIRouter()


# The largest id or page number worth querying for. Anything bigger cannot
# match a row, and SQLite refuses integers past 64 bits outright.
_MAX_INT = 2**31 - 1


def _parse_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    return number if 0 < number <= _MAX_INT else None


def _parse_ids(values: list[str]) -> tuple[int, ...]:
    """Distinct, valid ids from a repeated query parameter, in a stable order
    so the generated URLs do not churn."""
    return tuple(sorted({v for v in (_parse_int(x) for x in values) if v is not None}))


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

    def __init__(
        self,
        params: QueryParams,
        *,
        today: date,
        city_id: int | None = None,
        preset: str | None = None,
    ):
        # city_id, when given, pins the town (the city page) over any query value.
        # A pinned town is already in the path, so it stays out of the query
        # string: /city/x?city_id=5&page=2 was a second URL for /city/x?page=2.
        self.city_pinned = city_id is not None
        self.city_id = city_id if city_id is not None else _parse_int(params.get("city_id"))
        self.category_id = _parse_int(params.get("category_id"))
        self.source_id = _parse_int(params.get("source_id"))
        # Exclusions are the inverse of the two filters above: "everything
        # except these", repeatable so several can be hidden at once.
        self.exclude_category_ids = _parse_ids(params.getlist("exclude_category"))
        self.exclude_source_ids = _parse_ids(params.getlist("exclude_source"))
        self.search = (params.get("q") or "").strip() or None
        recurrence = params.get("recurrence")
        self.recurrence = recurrence if recurrence in ("single", "recurring") else None
        self.upcoming_only = params.get("upcoming_only") == "1"
        self.view = "map" if params.get("view") == "map" else "list"
        # preset, when given, pins the date window (a town's /today or
        # /this-weekend page). Like a pinned town it is in the path, so it stays
        # out of the query string that pagination and the view toggle reuse.
        self.preset_pinned = preset is not None
        if preset is None:
            preset = params.get("preset")
        self.preset = preset if preset in ("today", "weekend") else None

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
            "exclude_category_ids": self.exclude_category_ids,
            "exclude_source_ids": self.exclude_source_ids,
        }

    def query_string(self, *, include_pinned: bool = False) -> str:
        """The current filters as a URL query string (without page/view), so
        pagination, the view toggle, and the map fetch all stay shareable and
        in sync. A preset is emitted as ?preset=... rather than its expanded
        dates, keeping the URL tidy."""
        pairs: list[tuple[str, str]] = []
        if self.city_id and not self.city_pinned:
            pairs.append(("city_id", str(self.city_id)))
        if self.category_id:
            pairs.append(("category_id", str(self.category_id)))
        if self.source_id:
            pairs.append(("source_id", str(self.source_id)))
        pairs.extend(("exclude_category", str(i)) for i in self.exclude_category_ids)
        pairs.extend(("exclude_source", str(i)) for i in self.exclude_source_ids)
        if self.search:
            pairs.append(("q", self.search))
        if self.recurrence:
            pairs.append(("recurrence", self.recurrence))
        if self.upcoming_only:
            pairs.append(("upcoming_only", "1"))
        if self.preset and (include_pinned or not self.preset_pinned):
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
            "exclude_category_ids": self.exclude_category_ids,
            "exclude_source_ids": self.exclude_source_ids,
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
    noindex: bool = False,
    town: dict | None = None,
):
    settings = get_settings()
    today = town_today(selected_city) if selected_city is not None else local_today(db)
    page = _page_number(request)
    events, total, has_next = list_public_events(
        db, today=today, page=page, **filters.as_query_kwargs()
    )
    town = town or {}
    return render(
        request,
        "home.html",
        {
            "current_user": current_user,
            "can_access_admin": can_access_admin(db, current_user) if current_user else False,
            "registration_enabled": settings.registration_enabled,
            "events": events,
            # So the list can show a long-running event by when it ends.
            "today": today if isinstance(today, date) else today.default,
            "total": total,
            "page": page,
            "has_next": has_next,
            "per_page": PUBLIC_EVENTS_PER_PAGE,
            "cities": list_cities(db),
            "selected_city": selected_city,
            "categories": list_active_categories(db),
            "sources": list_public_sources(db, today=today, city_id=filters.city_id),
            "filters": filters.template_context(),
            "query_string": filters.query_string(),
            # The map fetches by query string alone, so it needs a pinned window too.
            "map_query_string": filters.query_string(include_pinned=True),
            "town": town,
            "base_path": base_path,
            # Each page of the listing is its own canonical page: pointing page
            # 2 at page 1 told search engines the later pages were duplicates.
            "canonical_url": seo.absolute_url(
                base_path if page == 1 else f"{base_path}?page={page}"
            ),
            "noindex": noindex,
            "listing_structured_data": (
                seo.city_listing_structured_data(
                    selected_city,
                    events,
                    page_url=seo.absolute_url(base_path),
                    name=town.get("heading"),
                    centers=town.get("centers"),
                )
                if selected_city
                else None
            ),
            "calendar_url": (
                seo.absolute_url(f"/city/{selected_city.slug}/events.ics")
                if selected_city
                else None
            ),
            "fallback_image_url": settings.public_fallback_image_url,
            "map_tile_url": settings.public_map_tile_url,
            "map_attribution": settings.public_map_attribution,
        },
    )


@router.get("/", response_class=HTMLResponse)
def home(request: Request, current_user: OptionalCurrentUser, db: DbSession):
    """Choose a college town. Deliberately not an events listing.

    This covers college towns, where "what is on" only means something once a
    town is chosen; a feed mixing Bloomington and Bethlehem is noise to a reader
    who lives in one of them. There is no route that lists every city's events
    together, and this page is the reason: the only way into the listing is
    through a city.
    """
    query = (request.query_params.get("q") or "").strip()
    counts = upcoming_counts_by_city(db, today=local_today(db))
    # A live town with nothing coming up is left off the list and out of the
    # suggestions: a card reading "0 upcoming events" sends a visitor to an empty
    # page. The town page itself stays reachable by its URL, and the town comes
    # back here on its own once it has an upcoming event. The filter reuses the
    # counts above, so it costs no extra query.
    cities = [c for c in search_public_cities(db, query=query) if counts.get(c.id)]

    # The whole town list travels with the page so the suggestions appear on the
    # keystroke rather than after a round trip. This is a few dozen towns; if it
    # ever reaches a few thousand, move it behind an endpoint.
    city_index = [
        {
            "name": city.name,
            "school": ", ".join(u.name for u in city.universities),
            "state": city.state_or_region or "",
            "slug": city.slug,
            "count": counts.get(city.id, 0),
        }
        for city in search_public_cities(db)
        if counts.get(city.id)
    ]
    return render(
        request,
        "city_chooser.html",
        {
            "current_user": current_user,
            "can_access_admin": can_access_admin(db, current_user) if current_user else False,
            "registration_enabled": get_settings().registration_enabled,
            "cities": cities,
            "query": query,
            "event_counts": counts,
            "city_index": city_index,
            "site_structured_data": seo.site_structured_data(),
            "organization_structured_data": seo.organization_structured_data(),
        },
    )


def _town_page(
    slug: str,
    request: Request,
    current_user,
    db,
    *,
    window: str | None = None,
):
    """A town's events: all upcoming, or one window of them (/today,
    /this-weekend). Each is a page of its own for search engines, titled after
    the search it answers ("things to do in Ann Arbor this weekend")."""
    suffix = f"/{window}" if window else ""
    city = get_city_by_slug(db, slug)
    if city is None:
        canonical = get_city_by_slug_ignoring_case(db, slug)
        if canonical is not None and canonical.is_active:
            # /city/BLOOMINGTON -> /city/bloomington, keeping the filters.
            # A relative Location, so the redirect never echoes the Host header.
            query = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(f"/city/{canonical.slug}{suffix}{query}", status_code=301)
    if city is None or not city.is_active:
        raise NotFoundError("City not found")
    # For the visitor log: which town this visitor is looking at.
    request.state.visit_town = city.slug
    # The city page pins the city filter, and a window page pins its dates.
    # Today in this town, not in UTC: at 8 PM in Bloomington UTC is already on
    # tomorrow, and "Today" showed tomorrow's events.
    today = town_today(city)
    preset = seo.TOWN_WINDOWS[window][0] if window else None
    filters = _Filters(request.query_params, today=today, city_id=city.id, preset=preset)
    counts = upcoming_counts_by_city(db, today=local_today(db))
    upcoming = counts.get(city.id, 0)
    weekend_from, weekend_to = this_weekend(today)
    _, weekend_count, _ = list_public_events(
        db, today=today, city_id=city.id, date_from=weekend_from, date_to=weekend_to,
        per_page=1,
    )
    window_count = None
    if window == "this-weekend":
        window_count = weekend_count
    elif window == "today":
        _, window_count, _ = list_public_events(
            db, today=today, city_id=city.id, date_from=today, date_to=today, per_page=1
        )
    centers = seo.town_centers(db)
    nearby = [
        {
            "city": other,
            "place": seo.town_place(other),
            "miles": round(miles),
            "count": counts.get(other.id, 0),
        }
        for other, miles in seo.nearby_towns(city, list_cities(db), centers=centers)
    ]
    base_path = seo.town_path(city, window)
    crumbs = [("Towns", "/"), (city.name, seo.town_path(city))]
    if window:
        crumbs.append((seo.TOWN_WINDOWS[window][1].capitalize(), base_path))
    town = {
        "window": window or "",
        "path": seo.town_path(city),
        "heading": seo.town_heading(city, window),
        "title": seo.town_page_title(city, window),
        "description": seo.town_meta_description(
            city, upcoming=upcoming, window=window, window_count=window_count
        ),
        "place": seo.town_place(city),
        "upcoming": upcoming,
        "weekend_count": weekend_count,
        "nearby": nearby,
        "centers": centers,
        "breadcrumb_structured_data": seo.breadcrumb_structured_data(crumbs),
        **_listings_checked(db, city, current_user),
    }
    return _render_events(
        request,
        db,
        current_user,
        filters,
        selected_city=city,
        base_path=base_path,
        # A window page is indexed exactly when its town is, so the town's
        # three pages join and leave search results together.
        noindex=not seo.town_is_indexable(upcoming),
        town=town,
    )


def _listings_checked(db, city, current_user=None) -> dict:
    """The town's "Listings checked ..." line, from its sources' run records
    (see app.services.event_freshness.town_listings_checked). Empty when no
    source has completed a successful check.

    When one source is holding the time back (its latest check failed, or it
    is overdue) while the others are current, the line names that source and
    says when every other source was checked, so a single stuck source is
    easy to spot. Administrators get a link to the source."""
    freshness = town_listings_checked(db, city.id)
    if freshness is None:
        return {"checked": "", "checked_iso": "", "checked_notes": [], "checked_holder": None}
    notes = []
    holder = None
    other_failing = freshness.failing
    if freshness.held_back:
        if freshness.held_back_by == "failing":
            other_failing -= 1
            rest = (
                "could not be checked on the latest try, so this is the time of its "
                "last successful check"
            )
        else:
            rest = "has not been checked since then"
        others = local_timestamp(freshness.others_checked_at, city.timezone)
        can_view = bool(current_user) and user_has_permission(db, current_user, "sites.view")
        holder = {
            "name": freshness.oldest_source_name,
            "rest": rest,
            "others": f"Every other source was checked by {others}",
            "admin_url": (
                f"/admin/websites/{freshness.oldest_website_id}" if can_view else ""
            ),
        }
    if other_failing:
        other = " other" if holder else ""
        notes.append(
            f"{_sources(other_failing, other)} could not be checked on the latest try"
        )
    if freshness.pending:
        has = "has" if freshness.pending == 1 else "have"
        notes.append(f"{_sources(freshness.pending)} {has} not completed a first check yet")
    return {
        "checked": local_timestamp(freshness.checked_at, city.timezone),
        "checked_iso": freshness.checked_at.isoformat(timespec="seconds"),
        "checked_notes": notes,
        "checked_holder": holder,
    }


def _sources(count: int, adjective: str = "") -> str:
    return f"{count}{adjective} source{'s' if count != 1 else ''}"


@router.get("/city/{slug}", response_class=HTMLResponse)
def city_page(slug: str, request: Request, current_user: OptionalCurrentUser, db: DbSession):
    return _town_page(slug, request, current_user, db)


@router.get("/city/{slug}/this-weekend", response_class=HTMLResponse)
def city_weekend_page(
    slug: str, request: Request, current_user: OptionalCurrentUser, db: DbSession
):
    return _town_page(slug, request, current_user, db, window="this-weekend")


@router.get("/city/{slug}/today", response_class=HTMLResponse)
def city_today_page(
    slug: str, request: Request, current_user: OptionalCurrentUser, db: DbSession
):
    return _town_page(slug, request, current_user, db, window="today")
