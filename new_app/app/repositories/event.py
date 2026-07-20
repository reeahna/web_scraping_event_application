from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.extraction.dedup import candidate_fingerprint
from app.extraction.types import EventCandidate
from app.models.event import Event
from app.schemas.event import EventCreate
from app.services.fingerprints import update_fingerprint_and_duplicates


def create_event(db: Session, data: EventCreate) -> Event:
    event = Event(**data.model_dump())
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
    return (
        db.query(Event)
        .filter(Event.website_id == website_id, Event.fingerprint == fingerprint)
        .first()
    )


def create_event_from_candidate(
    db: Session,
    candidate: EventCandidate,
    *,
    website_id: int,
    city_id: int | None,
    source: str,
) -> Event:
    event = Event(
        title=candidate.title,
        canonical_url=candidate.canonical_url,
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
        venue=candidate.venue,
        address=candidate.address,
        image_url=candidate.image_url,
        latitude=candidate.latitude,
        longitude=candidate.longitude,
        scraped_at=datetime.now(UTC),
        city_id=city_id,
        website_id=website_id,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    update_fingerprint_and_duplicates(db, event)
    db.refresh(event)
    return event


def update_event(db: Session, event: Event, candidate: EventCandidate) -> Event:
    """Overwrites source-controlled fields from a fresh extraction, but only
    where the candidate actually has a value — a transient extraction gap
    never nulls out a previously-good field. Every administrative decision
    (category override, location correction, duplicate resolution, and
    lifecycle state) is deliberately left untouched: a re-scrape must never
    silently reactivate an administratively deactivated or archived event,
    or erase a curator's override."""
    event.title = candidate.title
    event.canonical_url = candidate.canonical_url
    if candidate.description is not None:
        event.description = candidate.description
    if candidate.source_category is not None:
        event.source_category = candidate.source_category
    if candidate.timezone is not None:
        event.timezone = candidate.timezone
    if candidate.start_date is not None:
        event.start_date = candidate.start_date
    if candidate.end_date is not None:
        event.end_date = candidate.end_date
    if candidate.start_time is not None:
        event.start_time = candidate.start_time
    if candidate.end_time is not None:
        event.end_time = candidate.end_time
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
    event.scraped_at = datetime.now(UTC)
    db.commit()
    db.refresh(event)
    update_fingerprint_and_duplicates(db, event)
    db.refresh(event)
    return event
