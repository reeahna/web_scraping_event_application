"""Provider-independent notification system.

`notify()` is the one entry point every trigger call site uses (detection,
preview, run_extraction, approval, unsupported-report lifecycle). It fans
out one Notification row per recipient (never a single shared role-audience
row — see app.models.notification), then runs each row through the
configured delivery providers. A provider failure can never fail the
triggering business operation — every provider call is wrapped in its own
try/except.

Deduplication: skip creating anything (for any recipient) if a Notification
with the same `dedup_fingerprint` was created within
`NOTIFICATION_COOLDOWN_SECONDS`. The fingerprint already encodes the
triggering type + resource + relevant state, so an unchanged repeated
failure naturally collapses and a meaningful state change naturally
produces a different fingerprint and a new notification.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Protocol

from sqlalchemy.orm import Session

from app.models.notification import Notification
from app.models.user import User
from app.repositories.notification import create_notification, recent_notification_exists

logger = logging.getLogger("app.notifications")

NOTIFICATION_COOLDOWN_SECONDS = 6 * 60 * 60

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_ERROR = "error"
SEVERITY_SUCCESS = "success"


def build_dedup_fingerprint(*parts: str) -> str:
    encoded = "|".join(parts)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class NotificationProvider(Protocol):
    def send(self, notification: Notification, *, correlation_id: str | None) -> None: ...


class EmailNotificationProvider(Protocol):
    """Interface only this phase — no concrete production implementation.
    Real outbound email requires explicit configuration that doesn't exist
    yet; DevelopmentEmailNotificationProvider is the only implementation
    wired in, and it never leaves the process."""

    def send(self, notification: Notification) -> None: ...


class LogNotificationProvider:
    def send(self, notification: Notification, *, correlation_id: str | None) -> None:
        logger.info(
            "notification type=%s severity=%s resource=%s:%s correlation_id=%s",
            notification.notification_type,
            notification.severity,
            notification.related_resource_type,
            notification.related_resource_id,
            correlation_id,
        )


class DevelopmentEmailNotificationProvider:
    """Writes a structured [DEV-EMAIL] log line instead of sending real mail.
    Clearly non-production — there is no SMTP client, no outbound network
    call, nothing that could ever deliver to a real inbox."""

    def send(self, notification: Notification) -> None:
        logger.info(
            "[DEV-EMAIL] to=user:%s subject=%s body=%s",
            notification.recipient_user_id,
            notification.title,
            notification.message,
        )


_LOG_PROVIDER = LogNotificationProvider()
_EMAIL_PROVIDER: EmailNotificationProvider = DevelopmentEmailNotificationProvider()


def _deliver(db: Session, notification: Notification, *, correlation_id: str | None) -> None:
    try:
        _LOG_PROVIDER.send(notification, correlation_id=correlation_id)
    except Exception:
        logger.exception("Log notification provider failed for notification %s", notification.id)

    try:
        _EMAIL_PROVIDER.send(notification)
        notification.delivery_status = "sent"
    except Exception:
        logger.exception(
            "Development email notification provider failed for notification %s", notification.id
        )
        notification.delivery_status = "failed"
    notification.provider = "development_email"
    notification.delivery_attempts += 1
    db.commit()


def notify(
    db: Session,
    *,
    notification_type: str,
    severity: str,
    title: str,
    message: str,
    recipients: list[User],
    dedup_fingerprint: str,
    related_resource_type: str | None = None,
    related_resource_id: int | None = None,
    action_url: str | None = None,
    correlation_id: str | None = None,
) -> list[Notification]:
    if not recipients:
        return []
    if recent_notification_exists(
        db, dedup_fingerprint, cooldown_seconds=NOTIFICATION_COOLDOWN_SECONDS
    ):
        return []

    created: list[Notification] = []
    for user in recipients:
        notification = create_notification(
            db,
            recipient_user_id=user.id,
            notification_type=notification_type,
            severity=severity,
            title=title,
            message=message,
            related_resource_type=related_resource_type,
            related_resource_id=related_resource_id,
            action_url=action_url,
            dedup_fingerprint=dedup_fingerprint,
        )
        _deliver(db, notification, correlation_id=correlation_id)
        created.append(notification)
    return created
