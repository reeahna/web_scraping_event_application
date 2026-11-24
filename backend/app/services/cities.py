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


@dataclass
class StateGroup:
    """One state on the public chooser: its name and the towns listed under it."""

    name: str
    towns: list[City]
    upcoming: int


def group_towns_by_state(towns: list[City], counts: dict[int, int]) -> list[StateGroup]:
    """The chooser's towns grouped by state, states in alphabetical order.

    Towns are stored as "IN" or "Indiana", so both land under the full name
    (app.core.us_states.state_name). A town with no state goes under "Other".
    Only towns the caller passes are grouped: the chooser already left out
    towns with nothing coming up, so a state with none of those has no group.
    """
    from app.core.us_states import state_name

    groups: dict[str, StateGroup] = {}
    for town in towns:
        name = state_name(town.state_or_region) or OTHER_STATE
        group = groups.setdefault(name, StateGroup(name=name, towns=[], upcoming=0))
        group.towns.append(town)
        group.upcoming += counts.get(town.id, 0)
    return sorted(groups.values(), key=lambda g: (g.name == OTHER_STATE, g.name))


OTHER_STATE = "Other"
