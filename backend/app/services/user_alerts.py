"""The signed-in user's alerts inbox.

Alerts are written by app.services.alerts as notifications of type
"user_alert", one row per recipient. The admin notifications page is for
operational notices and needs admin access, so without this inbox a regular
user's in-app alerts were stored but never shown anywhere.

Everything here is scoped to one user id, so nobody can read or change
another user's alerts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.event import Event
from app.models.notification import Notification
from app.repositories.public_events import get_public_event_ids

USER_ALERT_TYPE = "user_alert"


@dataclass(frozen=True)
class InboxItem:
    notification: Notification
    event: Event | None
    # "open" when the alert's event still has a page; "past" once the event
    # has happened; "unavailable" when it was taken down. Alerts with no
    # event are always "open".
    event_state: str

    @property
    def is_unread(self) -> bool:
        return self.notification.read_at is None

    @property
    def is_linkable(self) -> bool:
        return self.event_state == "open" and bool(self.notification.action_url)


def _inbox_query(user_id: int):
    return select(Notification).where(
        Notification.recipient_user_id == user_id,
        Notification.notification_type == USER_ALERT_TYPE,
        Notification.dismissed_at.is_(None),
    )


def list_inbox(db: Session, *, user_id: int, today: date, limit: int = 100) -> list[InboxItem]:
    rows = list(
        db.scalars(
            _inbox_query(user_id)
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .limit(limit)
        )
    )
    event_ids = {
        n.related_resource_id
        for n in rows
        if n.related_resource_type == "event" and n.related_resource_id is not None
    }
    events = (
        {e.id: e for e in db.scalars(select(Event).where(Event.id.in_(event_ids)))}
        if event_ids
        else {}
    )
    public_ids = get_public_event_ids(db, list(event_ids), today=today)
    items = []
    for n in rows:
        if n.related_resource_type != "event":
            items.append(InboxItem(n, None, "open"))
            continue
        event = events.get(n.related_resource_id)
        last_day = event and (event.end_date or event.start_date)
        if last_day is not None and last_day < today:
            state = "past"
        elif n.related_resource_id in public_ids:
            state = "open"
        else:
            state = "unavailable"
        items.append(InboxItem(n, event, state))
    return items


def unread_count(db: Session, *, user_id: int) -> int:
    return db.scalar(
        select(func.count()).select_from(
            _inbox_query(user_id).where(Notification.read_at.is_(None)).subquery()
        )
    ) or 0


def get_own_alert(db: Session, *, user_id: int, notification_id: int) -> Notification | None:
    return db.scalar(_inbox_query(user_id).where(Notification.id == notification_id))


def mark_read(db: Session, notification: Notification) -> None:
    if notification.read_at is None:
        notification.read_at = datetime.now(UTC)
        db.commit()


def dismiss(db: Session, notification: Notification) -> None:
    now = datetime.now(UTC)
    notification.dismissed_at = now
    notification.read_at = notification.read_at or now
    db.commit()


def mark_all_read(db: Session, *, user_id: int) -> int:
    result = db.execute(
        update(Notification)
        .where(
            Notification.recipient_user_id == user_id,
            Notification.notification_type == USER_ALERT_TYPE,
            Notification.dismissed_at.is_(None),
            Notification.read_at.is_(None),
        )
        .values(read_at=datetime.now(UTC))
    )
    db.commit()
    return result.rowcount or 0
