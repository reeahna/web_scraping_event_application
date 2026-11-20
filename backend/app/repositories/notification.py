from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models.notification import Notification


def recent_notification_exists(
    db: Session, dedup_fingerprint: str, *, cooldown_seconds: int
) -> bool:
    cutoff = datetime.now(UTC) - timedelta(seconds=cooldown_seconds)
    return (
        db.query(Notification)
        .filter(
            Notification.dedup_fingerprint == dedup_fingerprint,
            Notification.created_at >= cutoff,
        )
        .first()
        is not None
    )


def create_notification(
    db: Session,
    *,
    recipient_user_id: int,
    notification_type: str,
    severity: str,
    title: str,
    message: str,
    related_resource_type: str | None,
    related_resource_id: int | None,
    action_url: str | None,
    dedup_fingerprint: str,
) -> Notification:
    notification = Notification(
        recipient_user_id=recipient_user_id,
        notification_type=notification_type,
        severity=severity,
        title=title,
        message=message,
        related_resource_type=related_resource_type,
        related_resource_id=related_resource_id,
        action_url=action_url,
        dedup_fingerprint=dedup_fingerprint,
    )
    db.add(notification)
    db.commit()
    db.refresh(notification)
    return notification


def get_notification(db: Session, notification_id: int) -> Notification | None:
    return db.get(Notification, notification_id)


# Newest first, with the primary key as a tie-breaker so rows created in the same
# instant always come back in the same order and paging never skips or repeats one.
_NEWEST_FIRST = (Notification.created_at.desc(), Notification.id.desc())

READ_STATE_UNREAD = "unread"
READ_STATE_READ = "read"


def list_notifications_for_user(
    db: Session, user_id: int, *, unread_only: bool = False, limit: int = 50
) -> list[Notification]:
    query = db.query(Notification).filter(Notification.recipient_user_id == user_id)
    if unread_only:
        query = query.filter(Notification.read_at.is_(None))
    return query.order_by(*_NEWEST_FIRST).limit(limit).all()


def page_notifications_for_user(
    db: Session,
    user_id: int,
    *,
    read_state: str | None = None,
    severity: str | None = None,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[Notification], int, int]:
    """One page of a user's notifications, newest first.

    `read_state` is "unread", "read" or None (both); `severity` is a stored
    severity value or None (any). Returns (rows, matching_total, page), where
    `page` is clamped to the last page that exists, so a page that emptied out
    (say, after marking its last unread row read) shows the nearest real page.
    Counting and fetching are two SQL queries; nothing beyond one page is loaded.
    """
    query = db.query(Notification).filter(Notification.recipient_user_id == user_id)
    if read_state == READ_STATE_UNREAD:
        query = query.filter(Notification.read_at.is_(None))
    elif read_state == READ_STATE_READ:
        query = query.filter(Notification.read_at.is_not(None))
    if severity:
        query = query.filter(Notification.severity == severity)

    total = query.order_by(None).count()
    last_page = max(1, -(-total // per_page))
    page = min(max(1, page), last_page)
    rows = query.order_by(*_NEWEST_FIRST).offset((page - 1) * per_page).limit(per_page).all()
    return rows, total, page


def count_unread_for_user(db: Session, user_id: int) -> int:
    return (
        db.query(Notification)
        .filter(Notification.recipient_user_id == user_id, Notification.read_at.is_(None))
        .count()
    )


def mark_read(db: Session, notification: Notification) -> Notification:
    if notification.read_at is None:
        notification.read_at = datetime.now(UTC)
        db.commit()
        db.refresh(notification)
    return notification


def mark_unread(db: Session, notification: Notification) -> Notification:
    """Clear `read_at` so the notification counts as unread again. Nothing else
    on the row changes."""
    if notification.read_at is not None:
        notification.read_at = None
        db.commit()
        db.refresh(notification)
    return notification


def mark_all_read_for_user(db: Session, user_id: int) -> int:
    """Mark every unread notification of one user as read in a single UPDATE.

    Uses the same "unread" definition as count_unread_for_user, so the admin
    nav's unread count drops to zero afterwards. Returns the number of rows
    changed.
    """
    result = db.execute(
        update(Notification)
        .where(Notification.recipient_user_id == user_id, Notification.read_at.is_(None))
        .values(read_at=datetime.now(UTC))
    )
    db.commit()
    return result.rowcount or 0
