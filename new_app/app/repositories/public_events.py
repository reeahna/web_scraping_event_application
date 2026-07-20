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
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.city import City
from app.models.event import Event
from app.models.website import Website

PUBLIC_EVENTS_PER_PAGE = 12


def current_public_date() -> date:
    settings = get_settings()
    return datetime.now(ZoneInfo(settings.app_timezone)).date()


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


def list_public_events(
    db: Session,
    *,
    today: date,
    city_id: int | None = None,
    category_id: int | None = None,
    upcoming_only: bool = False,
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = 1,
    per_page: int = PUBLIC_EVENTS_PER_PAGE,
) -> tuple[list[Event], int, bool]:
    query = _base_public_query(db, today=today)
    if city_id is not None:
        query = query.filter(Event.city_id == city_id)
    if category_id is not None:
        query = query.filter(
            or_(Event.category_id == category_id, Event.category_override_id == category_id)
        )
    if upcoming_only:
        # The base query already excludes past events; this narrows further
        # to strictly-future events (excludes ones starting today), making
        # the toggle a non-redundant restriction on top of "upcoming or ongoing".
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


def get_public_event(db: Session, event_id: int, *, today: date) -> Event | None:
    return _base_public_query(db, today=today).filter(Event.id == event_id).first()
