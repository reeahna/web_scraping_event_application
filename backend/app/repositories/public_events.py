"""The one reusable public-visibility predicate every public route goes
through (_base_public_query) — a hidden event can never be reachable via one
public path (listing) but not another (detail): get_public_event returning
None *is* the "not visible" answer, with no separate check needed.

"Today" is computed in one configured application timezone
(settings.app_timezone, UTC by default) rather than per-viewer or
per-event-city. An event's own city/venue may sit in a different timezone,
but reconciling that per-city is deliberately deferred past this MVP.
Comparisons are date-only (no time-of-day cutoff), so an event stays visible
for the entirety of its displayed day regardless of when during that day
it's viewed.

Occurrence-aware (Phase 12): a recurrence parent is never shown publicly — only
its concrete expanded occurrences (or a plain single event) appear — so a
series never renders as a parent card duplicating its occurrence cards.

Listed once: the same event is often listed by several sources (a theatre's
own site, the visitors' bureau and the local radio station), each under its
own URL, so their fingerprints never match. Listings, counts, the map and the
sitemap show one copy of a title on a date in a city (see
_without_repeat_listings). The other copies stay reachable at their own
detail URL and under their own source's filter.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, exists, func, not_, or_, select
from sqlalchemy.orm import Session, aliased

from app.config import get_settings
from app.models.city import City
from app.models.event import Event
from app.models.website import Website

PUBLIC_EVENTS_PER_PAGE = 12
# A hard cap on how many points a single map response may contain, so a broad
# filter can never build an unbounded payload.
MAX_MAP_POINTS = 2000


def current_public_date() -> date:
    settings = get_settings()
    return datetime.now(ZoneInfo(settings.app_timezone)).date()


def this_weekend(today: date) -> tuple[date, date]:
    """The upcoming (or current) weekend as an inclusive (Saturday, Sunday)
    date range. If today is already Sat/Sun, the current weekend is used."""
    weekday = today.weekday()  # Mon=0 .. Sun=6
    if weekday == 5:  # Saturday
        saturday = today
    elif weekday == 6:  # Sunday
        saturday = today - timedelta(days=1)
    else:
        saturday = today + timedelta(days=(5 - weekday))
    return saturday, saturday + timedelta(days=1)


def _visible(event, website, *, today: date) -> list:
    """The conditions for an event row (and its source row) to be public."""
    upcoming_or_ongoing = or_(
        and_(event.end_date.isnot(None), event.end_date >= today),
        and_(event.end_date.is_(None), event.start_date.isnot(None), event.start_date >= today),
    )
    return [
        event.is_active.is_(True),
        event.archived_at.is_(None),
        event.duplicate_status != "confirmed_duplicate",
        # Occurrence-aware: a recurrence parent is internal; the public sees
        # only concrete occurrences and single events.
        event.is_recurrence_parent.is_(False),
        website.is_active.is_(True),
        # `approved_pattern` is a JSON column: SQLAlchemy/SQLite store a
        # Python None there as the JSON literal 'null', not SQL NULL, so
        # `.isnot(None)` would never actually exclude an unapproved row.
        # `active_configuration_version` is a plain Integer set only at
        # approval time (see app.services.website_configuration.approve_configuration)
        # and stays NULL until then, so it's the SQL-safe proxy for "has
        # an approved configuration".
        website.active_configuration_version.isnot(None),
        upcoming_or_ongoing,
    ]


def _base_public_query(db: Session, *, today: date):
    return (
        db.query(Event)
        .join(Website, Event.website_id == Website.id)
        .join(City, Event.city_id == City.id)
        .filter(*_visible(Event, Website, today=today), City.is_active.is_(True))
    )


def _time_unknown(column):
    # Several sources give midnight when they have no time at all.
    return or_(column.is_(None), column == time(0, 0))


def _without_repeat_listings(query, *, today: date):
    """Leave out an event when an earlier-added public event has the same title
    (by Event.normalized_title, which ignores case and punctuation) on the same
    day in the same city, at the same time or with no time given on either.

    Two showings of a play on one day at different times both stay; the same
    showing on two sources becomes one card."""
    other = aliased(Event)
    other_website = aliased(Website)
    earlier_copy = (
        select(other.id)
        .join(other_website, other.website_id == other_website.id)
        .where(
            other.id < Event.id,
            other.normalized_title == Event.normalized_title,
            other.normalized_title != "",
            other.start_date == Event.start_date,
            other.city_id == Event.city_id,
            or_(
                _time_unknown(other.start_time),
                _time_unknown(Event.start_time),
                other.start_time == Event.start_time,
            ),
            *_visible(other, other_website, today=today),
        )
    )
    return query.filter(~exists(earlier_copy))


def upcoming_counts_by_city(db: Session, *, today: date) -> dict[int, int]:
    """How many publicly-visible upcoming events each city has, in one query.

    Uses the same base query as the listing, so a count can never promise
    events the city page would not then show.
    """
    rows = (
        _without_repeat_listings(_base_public_query(db, today=today), today=today)
        .with_entities(Event.city_id, func.count(Event.id))
        .group_by(Event.city_id)
        .all()
    )
    return {city_id: count for city_id, count in rows if city_id is not None}


def public_event_ids(db: Session, *, today: date, limit: int) -> list[int]:
    """Ids of the events the public site would currently show, newest first.

    Shares _base_public_query with the listing, so the sitemap can never
    advertise an event the site itself would not serve.
    """
    return [
        row.id
        for row in _without_repeat_listings(_base_public_query(db, today=today), today=today)
        .order_by(Event.start_date.asc(), Event.id.asc())
        .limit(limit)
        .all()
    ]


def _listing_query(db: Session, *, today: date, source_id: int | None):
    query = _base_public_query(db, today=today)
    # Filtered to one source, that source's own copy is the one to show.
    if source_id is None:
        query = _without_repeat_listings(query, today=today)
    return query


def _apply_filters(
    query,
    *,
    today: date,
    city_id: int | None,
    category_id: int | None,
    source_id: int | None,
    search: str | None,
    recurrence: str | None,
    upcoming_only: bool,
    date_from: date | None,
    date_to: date | None,
    exclude_category_ids: tuple[int, ...] = (),
    exclude_source_ids: tuple[int, ...] = (),
):
    if city_id is not None:
        query = query.filter(Event.city_id == city_id)
    if category_id is not None:
        query = query.filter(
            or_(Event.category_id == category_id, Event.category_override_id == category_id)
        )
    if source_id is not None:
        query = query.filter(Event.website_id == source_id)
    if exclude_category_ids:
        # Against the category the event is shown under: an admin override
        # wins over the scraped one. An uncategorised event is never excluded
        # (NOT IN on a NULL would otherwise drop it).
        effective = func.coalesce(Event.category_override_id, Event.category_id)
        query = query.filter(
            or_(effective.is_(None), not_(effective.in_(exclude_category_ids)))
        )
    if exclude_source_ids:
        query = query.filter(not_(Event.website_id.in_(exclude_source_ids)))
    if search:
        like = f"%{search.strip()}%"
        query = query.filter(
            or_(
                Event.title.ilike(like),
                Event.venue.ilike(like),
                Event.corrected_venue.ilike(like),
                Event.description.ilike(like),
            )
        )
    if recurrence == "recurring":
        query = query.filter(Event.recurrence_parent_id.isnot(None))
    elif recurrence == "single":
        query = query.filter(Event.recurrence_parent_id.is_(None))
    if upcoming_only:
        query = query.filter(Event.start_date.isnot(None), Event.start_date > today)
    if date_from is not None:
        query = query.filter(
            or_(
                Event.end_date >= date_from,
                and_(Event.end_date.is_(None), Event.start_date >= date_from),
            )
        )
    if date_to is not None:
        query = query.filter(Event.start_date.isnot(None), Event.start_date <= date_to)
    return query


def list_public_events(
    db: Session,
    *,
    today: date,
    city_id: int | None = None,
    category_id: int | None = None,
    source_id: int | None = None,
    search: str | None = None,
    recurrence: str | None = None,
    upcoming_only: bool = False,
    date_from: date | None = None,
    date_to: date | None = None,
    exclude_category_ids: tuple[int, ...] = (),
    exclude_source_ids: tuple[int, ...] = (),
    page: int = 1,
    per_page: int = PUBLIC_EVENTS_PER_PAGE,
) -> tuple[list[Event], int, bool]:
    query = _apply_filters(
        _listing_query(db, today=today, source_id=source_id),
        today=today, city_id=city_id, category_id=category_id, source_id=source_id,
        search=search, recurrence=recurrence, upcoming_only=upcoming_only,
        date_from=date_from, date_to=date_to,
        exclude_category_ids=exclude_category_ids, exclude_source_ids=exclude_source_ids,
    )
    total = query.count()
    page = max(page, 1)
    events = (
        query.order_by(Event.start_date.asc(), Event.id.asc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    has_next = page * per_page < total
    return events, total, has_next


def list_public_map_points(
    db: Session,
    *,
    today: date,
    city_id: int | None = None,
    category_id: int | None = None,
    source_id: int | None = None,
    search: str | None = None,
    recurrence: str | None = None,
    upcoming_only: bool = False,
    date_from: date | None = None,
    date_to: date | None = None,
    exclude_category_ids: tuple[int, ...] = (),
    exclude_source_ids: tuple[int, ...] = (),
) -> list[dict]:
    """Only visible, matching events that have usable public coordinates. The
    public coordinate (correction > source > geocoded) is computed in Python via
    the model property, and nothing sensitive is included in the payload."""
    query = _apply_filters(
        _listing_query(db, today=today, source_id=source_id),
        today=today, city_id=city_id, category_id=category_id, source_id=source_id,
        search=search, recurrence=recurrence, upcoming_only=upcoming_only,
        date_from=date_from, date_to=date_to,
        exclude_category_ids=exclude_category_ids, exclude_source_ids=exclude_source_ids,
    ).order_by(Event.start_date.asc(), Event.id.asc())

    points: list[dict] = []
    for event in query.limit(MAX_MAP_POINTS * 3):  # over-fetch; many lack coords
        lat, lng = event.public_latitude, event.public_longitude
        if lat is None or lng is None:
            continue
        category = event.effective_category
        points.append(
            {
                "id": event.id,
                "title": event.title,
                "url": f"/events/{event.id}",
                "latitude": lat,
                "longitude": lng,
                "start_date": event.start_date.isoformat() if event.start_date else None,
                "venue": event.public_venue,
                "category": category.name if category else None,
            }
        )
        if len(points) >= MAX_MAP_POINTS:
            break
    return points


def list_public_sources(
    db: Session, *, today: date, city_id: int | None = None
) -> list[Website]:
    """Websites that currently have at least one publicly-visible event, for the
    source filter. Scoped to a city when one is selected."""
    query = _base_public_query(db, today=today)
    if city_id is not None:
        query = query.filter(Event.city_id == city_id)
    website_ids = {event.website_id for event in query.all() if event.website_id is not None}
    if not website_ids:
        return []
    return (
        db.query(Website)
        .filter(Website.id.in_(website_ids))
        .order_by(Website.source_display_name, Website.name)
        .all()
    )


def get_public_event(db: Session, event_id: int, *, today: date) -> Event | None:
    return _base_public_query(db, today=today).filter(Event.id == event_id).first()
