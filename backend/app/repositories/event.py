from datetime import UTC, date, datetime, time

from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from app.extraction.dedup import candidate_fingerprint
from app.extraction.locations import has_venue
from app.extraction.normalize import (
    ALL_DAY_MARKER,
    END_ABSENT_MARKER,
    TIME_TBD_MARKER,
    plain_text,
)
from app.extraction.types import EventCandidate
from app.models.event import Event
from app.repositories.public_events import town_today
from app.schemas.event import EventCreate
from app.services.ai_label_state import requeue_if_inputs_changed
from app.services.categorization import assign_category
from app.services.event_freshness import mark_if_changed, visitor_snapshot
from app.services.fingerprints import update_fingerprint_and_duplicates


def create_event(db: Session, data: EventCreate) -> Event:
    event = Event(**data.model_dump())
    event.content_updated_at = datetime.now(UTC)
    db.add(event)
    db.commit()
    db.refresh(event)
    update_fingerprint_and_duplicates(db, event)
    db.refresh(event)
    return event


def get_event(db: Session, event_id: int) -> Event | None:
    return db.get(Event, event_id)


def list_events(
    db: Session, *, city_id: int | None = None, active_only: bool = True
) -> list[Event]:
    query = db.query(Event)
    if active_only:
        query = query.filter(Event.is_active.is_(True))
    if city_id is not None:
        query = query.filter(Event.city_id == city_id)
    return query.order_by(Event.start_date).all()


def described_event_urls(db: Session, website_id: int) -> frozenset[str]:
    """Canonical URLs of this website's events that already have a
    description, so extraction doesn't re-fetch their pages to find one."""
    rows = db.query(Event.canonical_url).filter(
        Event.website_id == website_id,
        Event.description.is_not(None),
        Event.description != "",
    )
    return frozenset(url for (url,) in rows)


def event_urls_with_venue(db: Session, website_id: int) -> frozenset[str]:
    """Canonical URLs of this website's events that already show a venue (the
    source's own or an administrator's correction), so extraction doesn't
    re-fetch their pages to find one. A placeholder ("TBA") doesn't count."""
    rows = db.query(Event.canonical_url, Event.venue, Event.corrected_venue).filter(
        Event.website_id == website_id,
        or_(
            and_(Event.venue.is_not(None), Event.venue != ""),
            and_(Event.corrected_venue.is_not(None), Event.corrected_venue != ""),
        ),
    )
    return frozenset(
        url
        for url, venue, corrected in rows
        if url and has_venue({"venue": corrected if corrected else venue})
    )


def event_urls_with_time(db: Session, website_id: int) -> frozenset[str]:
    """Canonical URLs of this website's events stored with a real clock time
    (not a bare 12:00 AM), so extraction doesn't re-fetch their pages for one."""
    rows = db.query(Event.canonical_url).filter(
        Event.website_id == website_id,
        Event.start_time.is_not(None),
        Event.start_time != time(0),
    )
    return frozenset(url for (url,) in rows if url)


def count_upcoming_events_not_seen(
    db: Session, website, *, seen_event_ids: set[int]
) -> int:
    """How many of this source's live, upcoming events a successful check did
    not see in the listing. A count for the run record only: a listing can be
    paged or windowed, so absence alone never hides an event."""
    today = date(*town_today(website.city).timetuple()[:3])
    rows = db.query(Event.id).filter(
        Event.website_id == website.id,
        Event.is_active.is_(True),
        Event.archived_at.is_(None),
        Event.is_recurrence_parent.is_(False),
        Event.is_cancelled.is_(False),
        or_(Event.end_date >= today, and_(Event.end_date.is_(None), Event.start_date >= today)),
    )
    return sum(1 for (event_id,) in rows if event_id not in seen_event_ids)


def find_existing_event_for_candidate(
    db: Session, candidate: EventCandidate, *, website_id: int, city_id: int | None
) -> Event | None:
    """Upsert lookup for the extraction engine: matches on the same
    fingerprint precedence app.services.fingerprints.event_fingerprint()
    already uses for a persisted Event (external source ID > canonical URL >
    composite), scoped to this website so a shared fingerprint with a
    *different* source's event is treated as a cross-source possible
    duplicate (Phase 5's existing workflow) rather than silently merged."""
    fingerprint = candidate_fingerprint(candidate, website_id=website_id, city_id=city_id)
    existing = (
        db.query(Event)
        .filter(Event.website_id == website_id, Event.fingerprint == fingerprint)
        .first()
    )
    if existing is not None:
        return existing
    return _row_keyed_by_series(db, candidate, website_id=website_id) or _session_row_for_event(
        db, candidate, website_id=website_id
    )


def _row_keyed_by_series(
    db: Session, candidate: EventCandidate, *, website_id: int
) -> Event | None:
    """A session keyed "<series id>@<date>" (see the LiveWhale pattern) whose
    series was stored before sessions had their own ids, as one row under the
    bare series id. The first session to arrive takes that row over (its id is
    rewritten by update_event), so the old row is corrected in place rather
    than left behind next to its sessions with stale dates."""
    parent = candidate.recurrence_parent_id
    own_id = candidate.external_source_id or ""
    if not parent or not own_id.startswith(f"{parent}@"):
        return None
    return (
        db.query(Event)
        .filter(Event.website_id == website_id, Event.external_source_id == parent)
        .order_by(Event.id)
        .first()
    )



def _session_row_for_event(
    db: Session, candidate: EventCandidate, *, website_id: int
) -> Event | None:
    """The reverse of _row_keyed_by_series: a single event (bare id) whose one
    occurrence was stored as a session "<id>@<date>", for instance when a run
    saw the record twice on overlapping feed pages. That row is taken over
    (update_event rewrites its id), keeping its id, history and corrections,
    instead of a second row being created beside it. Only when exactly one
    row has that key for the candidate's own date and it starts at the same
    clock time (or neither has a time), so a different session on that date is
    never taken."""
    own_id = candidate.external_source_id
    if (
        not own_id
        or "@" in own_id
        or candidate.recurrence_parent_id
        or candidate.start_date is None
    ):
        return None
    rows = (
        db.query(Event)
        .filter(
            Event.website_id == website_id,
            Event.external_source_id == f"{own_id}@{candidate.start_date.isoformat()}",
            Event.start_date == candidate.start_date,
        )
        .order_by(Event.id)
        .limit(2)
        .all()
    )
    if len(rows) != 1 or rows[0].start_time != candidate.start_time:
        return None
    return rows[0]

def create_event_from_candidate(
    db: Session,
    candidate: EventCandidate,
    *,
    website_id: int,
    city_id: int | None,
    source: str,
    commit: bool = True,
    fallback_url: str | None = None,
) -> Event:
    """A new event's details are new to visitors, so its content_updated_at
    starts now. ``fallback_url`` stands in for an event with no link of its own (an ICS
    feed identifies events by UID and may leave URL out), since every event
    row needs one. With ``commit=False`` the new row is only flushed (it has its id), and
    the caller commits: the import loop commits once per batch of events
    instead of three times per event (see app.services.extraction_runs)."""
    now = datetime.now(UTC)
    event = Event(
        title=candidate.title,
        canonical_url=candidate.canonical_url or fallback_url,
        source=source,
        external_source_id=candidate.external_source_id,
        description=candidate.description,
        source_category=candidate.source_category,
        timezone=candidate.timezone,
        origin="extracted",
        start_date=candidate.start_date,
        end_date=candidate.end_date,
        start_time=candidate.start_time,
        end_time=candidate.end_time,
        all_day=is_all_day_candidate(candidate),
        venue=candidate.venue,
        address=candidate.address,
        image_url=candidate.image_url,
        latitude=candidate.latitude,
        longitude=candidate.longitude,
        scraped_at=now,
        content_updated_at=now,
        city_id=city_id,
        website_id=website_id,
        occurrence_id=candidate.occurrence_id,
        recurrence_parent_id=candidate.recurrence_parent_id,
        is_recurrence_parent=candidate.is_recurrence_parent,
        is_cancelled=candidate.is_cancelled,
        # A cancelled occurrence is created deactivated (hidden), never dropped.
        is_active=not candidate.is_cancelled,
    )
    db.add(event)
    # Categorize from the active rules as part of this same transaction (a no-op
    # when no rules are configured).
    assign_category(db, event)
    if not commit:
        db.flush()
        update_fingerprint_and_duplicates(db, event, commit=False)
        return event
    db.commit()
    db.refresh(event)
    update_fingerprint_and_duplicates(db, event)
    db.refresh(event)
    return event


def update_event(
    db: Session, event: Event, candidate: EventCandidate, *, commit: bool = True
) -> Event:
    """Overwrites source-controlled fields from a fresh extraction, but only
    where the candidate actually has a value — a transient extraction gap
    never nulls out a previously-good field. Every administrative decision
    (category override, location correction, duplicate resolution, and
    lifecycle state) is deliberately left untouched: a re-scrape must never
    silently reactivate an administratively deactivated or archived event,
    or erase a curator's override.

    ``content_updated_at`` moves only when what a visitor sees differs after the
    update (see app.services.event_freshness.visitor_snapshot); a check that
    finds the same details leaves it alone. Columns set to the value they
    already hold are not written (SQLAlchemy leaves them out of the UPDATE)."""
    before = visitor_snapshot(event)
    event.title = candidate.title
    if candidate.canonical_url:
        event.canonical_url = candidate.canonical_url
    if candidate.description is not None:
        event.description = candidate.description
    if candidate.source_category is not None:
        event.source_category = candidate.source_category
    if candidate.timezone is not None:
        event.timezone = candidate.timezone
    keep_stored_time = _midnight_placeholder(event, candidate) or _full_day_placeholder(
        event, candidate
    )
    if candidate.start_date is not None:
        event.start_date = candidate.start_date
    if candidate.end_date is not None:
        event.end_date = candidate.end_date
    if candidate.start_time is not None and not keep_stored_time:
        event.start_time = candidate.start_time
    if candidate.end_time is not None and not keep_stored_time:
        event.end_time = candidate.end_time
    if END_ABSENT_MARKER in candidate.transformation_history and candidate.start_date is not None:
        # The source says the event has no end. An end stored by an earlier
        # run (a UTC reading of the start's hour, say) is stale, not data.
        event.end_date = None
        event.end_time = None
    if ALL_DAY_MARKER in candidate.transformation_history and candidate.start_date is not None:
        # The source says this is an all-day event, so a time stored by an
        # earlier run (a UTC midnight read as "5:00 AM") is stale, not data.
        event.start_time = None
        event.end_time = None
        event.all_day = True
    elif TIME_TBD_MARKER in candidate.transformation_history:
        # The source's all-day flag means "time not announced" for this site.
        event.start_time = None
        event.end_time = None
        event.all_day = False
    elif candidate.start_time is not None and not keep_stored_time:
        # The source now gives a clock time, so the event is no longer all-day.
        event.all_day = False
    if candidate.venue is not None:
        event.venue = candidate.venue
    if candidate.address is not None:
        event.address = candidate.address
    if candidate.image_url is not None:
        event.image_url = candidate.image_url
    if candidate.latitude is not None:
        event.latitude = candidate.latitude
    if candidate.longitude is not None:
        event.longitude = candidate.longitude
    if candidate.external_source_id is not None:
        event.external_source_id = candidate.external_source_id
    if candidate.occurrence_id is not None:
        event.occurrence_id = candidate.occurrence_id
    if candidate.recurrence_parent_id is not None:
        event.recurrence_parent_id = candidate.recurrence_parent_id
    elif (
        candidate.external_source_id is not None
        and event.recurrence_parent_id == candidate.external_source_id
    ):
        # A one-session "series" row taken over by the single event it really
        # is (see _session_row_for_event) no longer belongs to a series.
        event.recurrence_parent_id = None
    # An occurrence that has become cancelled is hidden safely on re-scrape:
    # deactivated and flagged, never deleted, and never silently reactivated
    # once cancelled.
    if candidate.is_cancelled:
        event.is_cancelled = True
        event.is_active = False
    event.scraped_at = datetime.now(UTC)
    # Re-categorize from the active rules on re-scrape. This sets the automatic
    # category only; an administrator's category override is a separate field and
    # is left untouched.
    assign_category(db, event)
    # New words for the model to read send an AI-labeled (or needs-review)
    # event back to the AI queue; its current category stays meanwhile.
    requeue_if_inputs_changed(event)
    mark_if_changed(event, before)
    if not commit:
        # The caller commits (once per batch in the import loop).
        update_fingerprint_and_duplicates(db, event, commit=False)
        return event
    db.commit()
    db.refresh(event)
    update_fingerprint_and_duplicates(db, event)
    db.refresh(event)
    return event


def is_all_day_candidate(candidate: EventCandidate) -> bool:
    """The source says this candidate lasts the whole day (normalize records
    that as ALL_DAY_MARKER and drops its clock times)."""
    return ALL_DAY_MARKER in candidate.transformation_history and candidate.start_date is not None


def _full_day_placeholder(event: Event, candidate: EventCandidate) -> bool:
    """True when an all-day event is re-scraped as 12:00 AM to 11:59 PM (or to
    midnight) on the same day without the source's all-day wording. That span
    says nothing new: it never turns an all-day event into a timed one. A real
    clock time (9:00 AM, say) does."""
    return bool(
        event.all_day
        and candidate.start_time == time(0)
        and candidate.end_time in (None, time(0), time(23, 59), time(23, 59, 59))
        and candidate.start_date is not None
        and candidate.start_date == event.start_date
        and candidate.end_date in (None, candidate.start_date)
    )


def _midnight_placeholder(event: Event, candidate: EventCandidate) -> bool:
    """True when a re-scrape offers 12:00 AM, with no end, for the day the
    event already has a real clock time on. A bare midnight with nothing else
    is how many listings write "no time given"; it never replaces a time the
    source stated before, nor turns an all-day event into a 12:00 AM one. (A
    midnight start is still stored for a new event, and an all-day event is
    handled by its own flag, never by midnight.)"""
    return (
        candidate.start_time == time(0)
        and candidate.end_time is None
        and candidate.start_date is not None
        and candidate.start_date == event.start_date
        and (event.start_time not in (None, time(0)) or event.all_day)
    )


_DECODED_FIELDS = ("title", "venue", "address")


def decode_stored_html_entities(db: Session, *, apply: bool) -> list[Event]:
    """Active events whose title, venue or address still holds an HTML entity
    ("Rock &amp; Roll"), saved before extraction decoded them. When `apply` is
    set they are decoded in place and their duplicate keys recomputed."""
    query = db.query(Event).filter(
        Event.is_active.is_(True),
        or_(*(getattr(Event, field).contains("&") for field in _DECODED_FIELDS)),
    )
    fixed = []
    for event in query.order_by(Event.id).all():
        changes = {}
        for field in _DECODED_FIELDS:
            value = getattr(event, field)
            if value and plain_text(value) != value:
                changes[field] = plain_text(value)
        if not changes:
            continue
        fixed.append(event)
        if apply:
            for field, value in changes.items():
                setattr(event, field, value)
            update_fingerprint_and_duplicates(db, event)
    return fixed
