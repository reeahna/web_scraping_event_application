from sqlalchemy.orm import Session

from app.models.event import Event
from app.schemas.event import EventCreate


def create_event(db: Session, data: EventCreate) -> Event:
    event = Event(**data.model_dump())
    db.add(event)
    db.commit()
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
