"""Alert generation and delivery (Phase 13).

Turns events into alerts for the users who follow the relevant city/category/
source or saved the event, honouring each user's preferences and channel
choices. Duplicates are prevented by the AlertDelivery ledger's unique
(user, alert_key, channel) constraint — an alert is recorded once and
re-recording is a no-op, so a re-scrape or retried digest never double-sends.

In-app delivery reuses the existing notification system (per-user fingerprint,
so one user's alert never suppresses another's). Email is queued or sent
through the pluggable EmailSender, which sends nothing unless email is
explicitly enabled and a real backend is configured.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.logging import get_logger
from app.models.event import Event
from app.models.user import User
from app.models.user_engagement import AlertDelivery, AlertPreference, SavedEvent
from app.services.email import EmailMessage, EmailSender, get_email_sender
from app.services.engagement import followers_of
from app.services.notifications import SEVERITY_INFO, notify

logger = get_logger("alerts")

_DIGEST_INTERVALS = {"daily": timedelta(days=1), "weekly": timedelta(days=7)}


def _record(
    db: Session, *, user_id: int, alert_key: str, channel: str, title: str, body: str,
    event_id: int | None, status: str,
) -> bool:
    """Insert a delivery row unless one already exists for this
    (user, alert_key, channel). Returns True when newly recorded."""
    exists = db.scalar(
        select(AlertDelivery.id).where(
            AlertDelivery.user_id == user_id,
            AlertDelivery.alert_key == alert_key,
            AlertDelivery.channel == channel,
        )
    )
    if exists is not None:
        return False
    db.add(
        AlertDelivery(
            user_id=user_id, alert_key=alert_key, channel=channel, status=status,
            title=title, body=body, event_id=event_id,
            sent_at=datetime.now(UTC) if status == "sent" else None,
        )
    )
    db.commit()
    return True


def _deliver_in_app(
    db: Session, user: User, *, alert_key: str, title: str, body: str, event: Event | None
) -> None:
    notify(
        db,
        notification_type="user_alert",
        severity=SEVERITY_INFO,
        title=title,
        message=body,
        recipients=[user],
        # Per-user fingerprint so one user's alert never dedups another's.
        dedup_fingerprint=f"alert:{user.id}:{alert_key}",
        related_resource_type="event" if event else None,
        related_resource_id=event.id if event else None,
        action_url=f"/events/{event.id}" if event else None,
    )


def _send_email_now(db: Session, user: User, sender: EmailSender, subject: str, body: str) -> bool:
    return sender.send(EmailMessage(to=user.email, subject=subject, body=body))


def _default_prefs() -> AlertPreference:
    """Sensible defaults for a user who follows/saved but never opened the
    preferences page: in-app on, email off, all alert types on. Transient — not
    added to the session — so it is read-only and creates no row."""
    return AlertPreference(
        user_id=0, in_app_enabled=True, email_enabled=False, frequency="immediate",
        notify_new_events=True, notify_reminders=True, notify_updates=True,
        unsubscribe_token="",
    )


def _prefs_for(db: Session, user_ids: set[int]) -> dict[int, AlertPreference]:
    if not user_ids:
        return {}
    rows = db.scalars(select(AlertPreference).where(AlertPreference.user_id.in_(user_ids)))
    found = {p.user_id: p for p in rows}
    # Fill in defaults for users with no stored preferences.
    return {uid: found.get(uid) or _default_prefs() for uid in user_ids}


def _users_by_id(db: Session, user_ids: set[int]) -> dict[int, User]:
    if not user_ids:
        return {}
    rows = db.scalars(select(User).where(User.id.in_(user_ids), User.is_active.is_(True)))
    return {u.id: u for u in rows}


def _deliver(
    db: Session, user: User, prefs: AlertPreference, *, alert_key: str, title: str, body: str,
    event: Event | None, sender: EmailSender,
) -> bool:
    delivered = False
    if prefs.in_app_enabled and _record(
        db, user_id=user.id, alert_key=alert_key, channel="in_app", title=title, body=body,
        event_id=event.id if event else None, status="sent",
    ):
        _deliver_in_app(db, user, alert_key=alert_key, title=title, body=body, event=event)
        delivered = True
    if prefs.email_enabled:
        immediate = prefs.frequency == "immediate"
        if _record(
            db, user_id=user.id, alert_key=alert_key, channel="email", title=title, body=body,
            event_id=event.id if event else None, status="sent" if immediate else "pending",
        ):
            if immediate:
                _send_email_now(db, user, sender, title, body)
            delivered = True
    return delivered


def on_new_event(db: Session, event: Event, *, sender: EmailSender | None = None) -> int:
    """Alert users who follow this event's city/category/source. Skips
    cancelled/inactive events. Returns the number of users alerted."""
    if event.is_cancelled or not event.is_active:
        return 0
    sender = sender or get_email_sender(get_settings())
    recipient_ids: set[int] = set()
    if event.city_id:
        recipient_ids.update(followers_of(db, follow_type="city", target_id=event.city_id))
    for cat_id in (event.category_id, event.category_override_id):
        if cat_id:
            recipient_ids.update(followers_of(db, follow_type="category", target_id=cat_id))
    if event.website_id:
        recipient_ids.update(followers_of(db, follow_type="source", target_id=event.website_id))

    prefs = _prefs_for(db, recipient_ids)
    users = _users_by_id(db, recipient_ids)
    title = f"New event: {event.title}"
    body = f"{event.title} on {event.start_date} at {event.public_venue or 'a venue near you'}."
    count = 0
    for user_id in recipient_ids:
        pref, user = prefs.get(user_id), users.get(user_id)
        if pref is None or user is None or not pref.notify_new_events:
            continue
        if _deliver(
            db, user, pref, alert_key=f"new_event:{event.id}", title=title, body=body,
            event=event, sender=sender,
        ):
            count += 1
    return count


def on_event_change(
    db: Session, event: Event, *, kind: str, sender: EmailSender | None = None
) -> int:
    """Alert users who saved this event about a meaningful update or a
    cancellation. `kind` is 'updated' or 'cancelled'."""
    sender = sender or get_email_sender(get_settings())
    saver_ids = set(
        db.scalars(select(SavedEvent.user_id).where(SavedEvent.event_id == event.id))
    )
    prefs = _prefs_for(db, saver_ids)
    users = _users_by_id(db, saver_ids)
    if kind == "cancelled":
        title = f"Cancelled: {event.title}"
        body = f"{event.title} on {event.start_date} has been cancelled."
    else:
        title = f"Updated: {event.title}"
        body = f"Details changed for {event.title} on {event.start_date}."
    count = 0
    for user_id in saver_ids:
        pref, user = prefs.get(user_id), users.get(user_id)
        if pref is None or user is None or not pref.notify_updates:
            continue
        if _deliver(
            db, user, pref, alert_key=f"{kind}:{event.id}", title=title, body=body,
            event=event, sender=sender,
        ):
            count += 1
    return count


def send_saved_event_reminders(
    db: Session, *, now: datetime | None = None, sender: EmailSender | None = None
) -> int:
    """Remind users of saved events starting the next day. Deduped per
    (user, event) so a reminder is sent at most once."""
    now = now or datetime.now(UTC)
    sender = sender or get_email_sender(get_settings())
    target = now.date() + timedelta(days=1)
    rows = db.execute(
        select(SavedEvent.user_id, Event)
        .join(Event, Event.id == SavedEvent.event_id)
        .where(Event.start_date == target, Event.is_active.is_(True))
    ).all()
    user_ids = {user_id for user_id, _ in rows}
    prefs = _prefs_for(db, user_ids)
    users = _users_by_id(db, user_ids)
    count = 0
    for user_id, event in rows:
        pref, user = prefs.get(user_id), users.get(user_id)
        if pref is None or user is None or not pref.notify_reminders:
            continue
        title = f"Reminder: {event.title} is tomorrow"
        body = f"{event.title} starts {event.start_date}."
        if _deliver(
            db, user, pref, alert_key=f"reminder:{event.id}", title=title, body=body,
            event=event, sender=sender,
        ):
            count += 1
    return count


def send_pending_digests(
    db: Session, *, now: datetime | None = None, sender: EmailSender | None = None
) -> int:
    """Batch each user's pending email alerts into a single digest once their
    daily/weekly interval has elapsed. Returns the number of digests sent."""
    now = now or datetime.now(UTC)
    sender = sender or get_email_sender(get_settings())
    prefs_rows = db.scalars(
        select(AlertPreference).where(
            AlertPreference.email_enabled.is_(True),
            AlertPreference.frequency.in_(tuple(_DIGEST_INTERVALS)),
        )
    )
    sent = 0
    for prefs in prefs_rows:
        interval = _DIGEST_INTERVALS[prefs.frequency]
        last = prefs.last_digest_at
        if last is not None and (last.tzinfo and now - last < interval):
            continue
        pending = list(
            db.scalars(
                select(AlertDelivery).where(
                    AlertDelivery.user_id == prefs.user_id,
                    AlertDelivery.channel == "email",
                    AlertDelivery.status == "pending",
                )
            )
        )
        if not pending:
            continue
        user = db.get(User, prefs.user_id)
        if user is not None:
            lines = "\n".join(f"- {d.title}" for d in pending)
            _send_email_now(
                db, user, sender, f"Your events digest ({len(pending)})", lines
            )
        for delivery in pending:
            delivery.status = "sent"
            delivery.sent_at = now
        prefs.last_digest_at = now
        db.commit()
        sent += 1
    return sent
