from sqlalchemy.orm import Session

from app.models.event_category import EventCategory


def list_active_categories(db: Session) -> list[EventCategory]:
    return (
        db.query(EventCategory)
        .filter(EventCategory.is_active.is_(True))
        .order_by(EventCategory.display_order, EventCategory.name)
        .all()
    )
