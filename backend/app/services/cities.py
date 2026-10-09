from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.city import City
from app.models.event import Event
from app.models.website import Website


@dataclass
class CityDeletionImpact:
    total_events: int
    archived_events: int
    total_websites: int
    archived_websites: int

    @property
    def unarchived_events(self) -> int:
        return self.total_events - self.archived_events

    @property
    def unarchived_websites(self) -> int:
        return self.total_websites - self.archived_websites

    @property
    def blocked_by_events(self) -> bool:
        return self.unarchived_events > 0

    @property
    def blocked_by_websites(self) -> bool:
        return self.unarchived_websites > 0

    @property
    def can_delete(self) -> bool:
        return not self.blocked_by_events and not self.blocked_by_websites


def get_deletion_impact(db: Session, city: City) -> CityDeletionImpact:
    total_events = db.query(Event).filter(Event.city_id == city.id).count()
    archived_events = (
        db.query(Event).filter(Event.city_id == city.id, Event.archived_at.isnot(None)).count()
    )
    total_websites = db.query(Website).filter(Website.city_id == city.id).count()
    archived_websites = (
        db.query(Website)
        .filter(Website.city_id == city.id, Website.archived_at.isnot(None))
        .count()
    )
    return CityDeletionImpact(
        total_events=total_events,
        archived_events=archived_events,
        total_websites=total_websites,
        archived_websites=archived_websites,
    )


def archive_city_events(db: Session, city: City) -> int:
    """Archive all not-yet-archived events for this city. Returns the count archived."""
    now = datetime.now(UTC)
    events = db.query(Event).filter(Event.city_id == city.id, Event.archived_at.is_(None)).all()
    for event in events:
        event.archived_at = now
        event.is_active = False
    db.commit()
    return len(events)


def delete_archived_city_events(db: Session, city: City) -> int:
    """Permanently delete already-archived events for this city. Returns the count deleted."""
    events = db.query(Event).filter(Event.city_id == city.id, Event.archived_at.isnot(None)).all()
    count = len(events)
    for event in events:
        db.delete(event)
    db.commit()
    return count


def archive_city_websites(db: Session, city: City) -> int:
    """Archive all not-yet-archived websites for this city. Returns the count archived."""
    now = datetime.now(UTC)
    websites = (
        db.query(Website).filter(Website.city_id == city.id, Website.archived_at.is_(None)).all()
    )
    for website in websites:
        website.archived_at = now
    db.commit()
    return len(websites)
