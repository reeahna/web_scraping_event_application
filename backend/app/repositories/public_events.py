"""The one reusable public-visibility predicate every public route goes
through (_base_public_query) — a hidden event can never be reachable via one
public path (listing) but not another (detail): get_public_event returning
None *is* the "not visible" answer, with no separate check needed.

"Today" is each town's own calendar date. Event dates and times are stored as
the event's local wall clock, so they are compared with the date in the
event's town (City.timezone), never with UTC's: at 8 PM in Bloomington UTC is
already on tomorrow, and comparing with it hid tonight's events and filled
"Today" with tomorrow's. A town page passes its town's date (town_today); a
listing across towns passes a LocalDates, which the queries turn into a
per-town date (a CASE on City.timezone). Comparisons are date-only (no
time-of-day cutoff), so an event stays visible for the whole of its local day.

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

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import Date, and_, case, exists, func, literal, not_, or_, select
from sqlalchemy.orm import Session, aliased

from app.config import get_settings
from app.core.search import LIKE_ESCAPE, contains_pattern
from app.core.timezones import local_date
from app.models.city import City
from app.models.event import Event
from app.models.website import Website

PUBLIC_EVENTS_PER_PAGE = 12
# A hard cap on how many points a single map response may contain, so a broad
# filter can never build an unbounded payload.
MAX_MAP_POINTS = 2000


def _utcnow() -> datetime:
    """The current instant (one place, so tests can set the clock)."""
    return datetime.now(UTC)


def current_public_date() -> date:
    """The date in the configured application timezone (UTC by default). Only
    for places with no town to go by; public queries take town_today() or
    local_today() instead."""
    settings = get_settings()
    return local_date(settings.app_timezone, _utcnow())


def town_today(city: City | None, now: datetime | None = None) -> date:
    """Today's date in this town."""
    if city is None:
        return current_public_date()
    return local_date(city.timezone, now or _utcnow())


@dataclass(frozen=True)
class LocalDates:
    """A calendar date per town timezone, for queries across towns: "today"
    (or a weekend's Saturday) is not the same date everywhere at once.

    `by_zone` maps each City.timezone value as stored to its date; `default`
    (the application timezone's date) serves any other zone."""

    by_zone: dict[str, date]
    default: date

    def for_zone(self, zone: str | None) -> date:
        return self.by_zone.get(zone or "", self.default)

    def shifted(self, days: int) -> LocalDates:
        return LocalDates(
            {zone: day + timedelta(days=days) for zone, day in self.by_zone.items()},
            self.default + timedelta(days=days),
        )

    def map(self, fn) -> LocalDates:
        return LocalDates({zone: fn(day) for zone, day in self.by_zone.items()}, fn(self.default))

    def isoformat(self) -> str:
        return self.default.isoformat()

    def sql(self, zone_column):
        """This date for each row, by the row's town timezone."""
        if not self.by_zone:
            return literal(self.default, Date)
        return case(
            {zone: literal(day, Date) for zone, day in sorted(self.by_zone.items())},
            value=zone_column,
            else_=literal(self.default, Date),
        )


def local_today(db: Session, now: datetime | None = None) -> LocalDates:
    """Today's date in every town's timezone at the same instant."""
    moment = now or _utcnow()
    zones = {zone for (zone,) in db.query(City.timezone).distinct() if zone}
    return LocalDates(
        {zone: local_date(zone, moment) for zone in zones},
        local_date(get_settings().app_timezone, moment),
    )


DateBound = date | LocalDates


def today_for_event(today: DateBound, event: Event) -> date:
    """`today` as the calendar date in this event's town."""
    if isinstance(today, LocalDates):
        city = getattr(event, "city", None)
        return today.for_zone(city.timezone if city is not None else None)
    return today


def _bound(value: DateBound, zone_column=None):
    """A date bound as SQL: a plain date as itself, a LocalDates per row."""
    if isinstance(value, LocalDates):
        return value.sql(City.timezone if zone_column is None else zone_column)
    return value


def _weekend_from(today: date) -> date:
    weekday = today.weekday()  # Mon=0 .. Sun=6
    if weekday == 5:  # Saturday
        return today
    if weekday == 6:  # Sunday
        return today - timedelta(days=1)
    return today + timedelta(days=(5 - weekday))


def this_weekend(today: DateBound) -> tuple[DateBound, DateBound]:
    """The upcoming (or current) weekend as an inclusive (Saturday, Sunday)
    date range. If today is already Sat/Sun, the current weekend is used.
    For a LocalDates, each town gets its own weekend."""
    if isinstance(today, LocalDates):
        saturday = today.map(_weekend_from)
        return saturday, saturday.shifted(1)
    saturday = _weekend_from(today)
    return saturday, saturday + timedelta(days=1)


def _visible(event, website, *, today: DateBound) -> list:
    """The conditions for an event row (and its source row) to be public."""
    today = _bound(today)
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


def _base_public_query(db: Session, *, today: DateBound):
    return (
        db.query(Event)
        .join(Website, Event.website_id == Website.id)
        .join(City, Event.city_id == City.id)
        .filter(*_visible(Event, Website, today=today), City.is_active.is_(True))
    )


def _time_unknown(column):
    # Several sources give midnight when they have no time at all.
    return or_(column.is_(None), column == time(0, 0))


def _without_repeat_listings(query, *, today: DateBound, filters: dict | None = None):
    """Leave out an event when an earlier-added public event has the same title
    (by Event.normalized_title, which ignores case and punctuation) on the same
    day in the same city, at the same time or with no time given on either.

    Two showings of a play on one day at different times both stay; the same
    showing on two sources becomes one card.

    `filters` are the visitor's filters (see _filter_conditions). Only a copy
    that passes them counts as "earlier": otherwise a filter that drops the
    kept copy (excluding its source, say) would hide every copy of the event."""
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
            *(_filter_conditions(other, today=today, **filters) if filters else ()),
        )
    )
    return query.filter(~exists(earlier_copy))


def upcoming_counts_by_city(db: Session, *, today: DateBound) -> dict[int, int]:
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


def public_event_ids(db: Session, *, today: DateBound, limit: int) -> list[int]:
    """Ids of the events the public site would currently show, newest first.

    Shares _base_public_query with the listing, so the sitemap can never
    advertise an event the site itself would not serve.
    """
    return [
        row.id
        for row in _without_repeat_listings(_base_public_query(db, today=today), today=today)
        .order_by(*_chronological())
        .limit(limit)
        .all()
    ]


def _chronological(entity=Event):
    """Soonest first by local date, then by start time (events without one
    after the timed ones that day), then oldest row first for a stable order."""
    return (
        entity.start_date.asc(),
        case((entity.start_time.is_(None), 1), else_=0),
        entity.start_time.asc(),
        entity.id.asc(),
    )


def _listing_query(db: Session, *, today: DateBound, **filters):
    """The public listing with the visitor's filters applied."""
    query = _base_public_query(db, today=today)
    # Filtered to one source, that source's own copy is the one to show.
    if filters.get("source_id") is None:
        query = _without_repeat_listings(query, today=today, filters=filters)
    return query.filter(*_filter_conditions(Event, today=today, **filters))


def _filter_conditions(
    entity,
    *,
    today: DateBound,
    city_id: int | None,
    category_id: int | None,
    source_id: int | None,
    search: str | None,
    recurrence: str | None,
    upcoming_only: bool,
    date_from: DateBound | None,
    date_to: DateBound | None,
    exclude_category_ids: tuple[int, ...] = (),
    exclude_source_ids: tuple[int, ...] = (),
) -> list:
    """The visitor's filters as WHERE conditions on `entity` (Event, or an alias
    of it), so the repeat-listing check can hold the other copy to them too."""
    conditions = []
    if city_id is not None:
        conditions.append(entity.city_id == city_id)
    if category_id is not None:
        conditions.append(
            or_(entity.category_id == category_id, entity.category_override_id == category_id)
        )
    if source_id is not None:
        conditions.append(entity.website_id == source_id)
    if exclude_category_ids:
        # Against the category the event is shown under: an admin override
        # wins over the scraped one. An uncategorised event is never excluded
        # (NOT IN on a NULL would otherwise drop it).
        effective = func.coalesce(entity.category_override_id, entity.category_id)
        conditions.append(
            or_(effective.is_(None), not_(effective.in_(exclude_category_ids)))
        )
    if exclude_source_ids:
        conditions.append(not_(entity.website_id.in_(exclude_source_ids)))
    if search:
        like = contains_pattern(search.strip())
        conditions.append(
            or_(
                entity.title.ilike(like, escape=LIKE_ESCAPE),
                entity.venue.ilike(like, escape=LIKE_ESCAPE),
                entity.corrected_venue.ilike(like, escape=LIKE_ESCAPE),
                entity.description.ilike(like, escape=LIKE_ESCAPE),
            )
        )
    if recurrence == "recurring":
        conditions.append(entity.recurrence_parent_id.isnot(None))
    elif recurrence == "single":
        conditions.append(entity.recurrence_parent_id.is_(None))
    if upcoming_only:
        conditions.append(
            and_(entity.start_date.isnot(None), entity.start_date > _bound(today))
        )
    if date_from is not None:
        date_from = _bound(date_from)
        conditions.append(
            or_(
                entity.end_date >= date_from,
                and_(entity.end_date.is_(None), entity.start_date >= date_from),
            )
        )
    if date_to is not None:
        conditions.append(
            and_(entity.start_date.isnot(None), entity.start_date <= _bound(date_to))
        )
    return conditions


def list_public_events(
    db: Session,
    *,
    today: DateBound,
    city_id: int | None = None,
    category_id: int | None = None,
    source_id: int | None = None,
    search: str | None = None,
    recurrence: str | None = None,
    upcoming_only: bool = False,
    date_from: DateBound | None = None,
    date_to: DateBound | None = None,
    exclude_category_ids: tuple[int, ...] = (),
    exclude_source_ids: tuple[int, ...] = (),
    page: int = 1,
    per_page: int = PUBLIC_EVENTS_PER_PAGE,
) -> tuple[list[Event], int, bool]:
    query = _listing_query(
        db,
        today=today, city_id=city_id, category_id=category_id, source_id=source_id,
        search=search, recurrence=recurrence, upcoming_only=upcoming_only,
        date_from=date_from, date_to=date_to,
        exclude_category_ids=exclude_category_ids, exclude_source_ids=exclude_source_ids,
    )
    total = query.count()
    page = max(page, 1)
    events = (
        query.order_by(*_chronological())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    has_next = page * per_page < total
    return events, total, has_next


def list_public_map_points(
    db: Session,
    *,
    today: DateBound,
    city_id: int | None = None,
    category_id: int | None = None,
    source_id: int | None = None,
    search: str | None = None,
    recurrence: str | None = None,
    upcoming_only: bool = False,
    date_from: DateBound | None = None,
    date_to: DateBound | None = None,
    exclude_category_ids: tuple[int, ...] = (),
    exclude_source_ids: tuple[int, ...] = (),
) -> list[dict]:
    """Only visible, matching events that have usable public coordinates. The
    public coordinate (correction > source > geocoded) is computed in Python via
    the model property, and nothing sensitive is included in the payload."""
    query = _listing_query(
        db,
        today=today, city_id=city_id, category_id=category_id, source_id=source_id,
        search=search, recurrence=recurrence, upcoming_only=upcoming_only,
        date_from=date_from, date_to=date_to,
        exclude_category_ids=exclude_category_ids, exclude_source_ids=exclude_source_ids,
    ).order_by(*_chronological())

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
    db: Session, *, today: DateBound, city_id: int | None = None
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


def get_public_event(db: Session, event_id: int, *, today: DateBound) -> Event | None:
    return _base_public_query(db, today=today).filter(Event.id == event_id).first()


def get_public_event_ids(db: Session, event_ids: list[int], *, today: DateBound) -> set[int]:
    """Which of these event ids are public today (the ones get_public_event finds)."""
    if not event_ids:
        return set()
    rows = _base_public_query(db, today=today).filter(Event.id.in_(event_ids))
    return {event_id for (event_id,) in rows.with_entities(Event.id)}
