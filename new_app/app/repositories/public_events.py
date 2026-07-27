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
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

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


def _base_public_query(db: Session, *, today: date):
    upcoming_or_ongoing = or_(
        and_(Event.end_date.isnot(None), Event.end_date >= today),
        and_(Event.end_date.is_(None), Event.start_date.isnot(None), Event.start_date >= today),
    )
    return (
        db.query(Event)
        .join(Website, Event.website_id == Website.id)
        .join(City, Event.city_id == City.id)
        .filter(
            Event.is_active.is_(True),
            Event.archived_at.is_(None),
            Event.duplicate_status != "confirmed_duplicate",
            # Occurrence-aware: a recurrence parent is internal; the public sees
            # only concrete occurrences and single events.
            Event.is_recurrence_parent.is_(False),
            Website.is_active.is_(True),
            # `approved_pattern` is a JSON column: SQLAlchemy/SQLite store a
            # Python None there as the JSON literal 'null', not SQL NULL, so
            # `.isnot(None)` would never actually exclude an unapproved row.
            # `active_configuration_version` is a plain Integer set only at
            # approval time (see app.services.website_configuration.approve_configuration)
            # and stays NULL until then, so it's the SQL-safe proxy for "has
            # an approved configuration".
            Website.active_configuration_version.isnot(None),
            City.is_active.is_(True),
            upcoming_or_ongoing,
        )
    )


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
):
    if city_id is not None:
        query = query.filter(Event.city_id == city_id)
    if category_id is not None:
        query = query.filter(
            or_(Event.category_id == category_id, Event.category_override_id == category_id)
        )
    if source_id is not None:
        query = query.filter(Event.website_id == source_id)
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
    page: int = 1,
    per_page: int = PUBLIC_EVENTS_PER_PAGE,
) -> tuple[list[Event], int, bool]:
    query = _apply_filters(
        _base_public_query(db, today=today),
        today=today, city_id=city_id, category_id=category_id, source_id=source_id,
        search=search, recurrence=recurrence, upcoming_only=upcoming_only,
        date_from=date_from, date_to=date_to,
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
) -> list[dict]:
    """Only visible, matching events that have usable public coordinates. The
    public coordinate (correction > source > geocoded) is computed in Python via
    the model property, and nothing sensitive is included in the payload."""
    query = _apply_filters(
        _base_public_query(db, today=today),
        today=today, city_id=city_id, category_id=category_id, source_id=source_id,
        search=search, recurrence=recurrence, upcoming_only=upcoming_only,
        date_from=date_from, date_to=date_to,
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
