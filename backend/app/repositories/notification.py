from datetime import UTC, datetime, timedelta

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


def list_notifications_for_user(
    db: Session, user_id: int, *, unread_only: bool = False, limit: int = 50
) -> list[Notification]:
    query = db.query(Notification).filter(Notification.recipient_user_id == user_id)
    if unread_only:
        query = query.filter(Notification.read_at.is_(None))
    return query.order_by(Notification.created_at.desc()).limit(limit).all()


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


def dismiss(db: Session, notification: Notification) -> Notification:
    notification.dismissed_at = datetime.now(UTC)
    db.commit()
    db.refresh(notification)
    return notification
