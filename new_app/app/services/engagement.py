"""Saved events, follows, and alert preferences (Phase 13).

Every function takes the acting user and scopes strictly to them, so one user's
saved events, follows, or preferences can never be read or changed by another.
"""

from __future__ import annotations

import secrets

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.event import Event
from app.models.user_engagement import (
    ALERT_FREQUENCIES,
    AlertPreference,
    SavedEvent,
    UserFollow,
)

_FOLLOW_TYPES = {"city", "category", "source"}


# --- saved events ------------------------------------------------------------


def save_event(db: Session, *, user_id: int, event_id: int) -> SavedEvent:
    existing = db.scalar(
        select(SavedEvent).where(
            SavedEvent.user_id == user_id, SavedEvent.event_id == event_id
        )
    )
    if existing is not None:
        return existing
    saved = SavedEvent(user_id=user_id, event_id=event_id)
    db.add(saved)
    db.commit()
    db.refresh(saved)
    return saved


def unsave_event(db: Session, *, user_id: int, event_id: int) -> None:
    db.execute(
        delete(SavedEvent).where(
            SavedEvent.user_id == user_id, SavedEvent.event_id == event_id
        )
    )
    db.commit()


def is_event_saved(db: Session, *, user_id: int, event_id: int) -> bool:
    return (
        db.scalar(
            select(SavedEvent.id).where(
                SavedEvent.user_id == user_id, SavedEvent.event_id == event_id
            )
        )
        is not None
    )


def list_saved_events(db: Session, *, user_id: int) -> list[Event]:
    return list(
        db.scalars(
            select(Event)
            .join(SavedEvent, SavedEvent.event_id == Event.id)
            .where(SavedEvent.user_id == user_id)
            .order_by(Event.start_date.asc(), Event.id.asc())
        )
    )


def saved_event_ids(db: Session, *, user_id: int) -> set[int]:
    return set(db.scalars(select(SavedEvent.event_id).where(SavedEvent.user_id == user_id)))


# --- follows -----------------------------------------------------------------


def follow(db: Session, *, user_id: int, follow_type: str, target_id: int) -> UserFollow:
    if follow_type not in _FOLLOW_TYPES:
        raise ValueError(f"unknown follow type: {follow_type}")
    existing = db.scalar(
        select(UserFollow).where(
            UserFollow.user_id == user_id,
            UserFollow.follow_type == follow_type,
            UserFollow.target_id == target_id,
        )
    )
    if existing is not None:
        return existing
    row = UserFollow(user_id=user_id, follow_type=follow_type, target_id=target_id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def unfollow(db: Session, *, user_id: int, follow_type: str, target_id: int) -> None:
    db.execute(
        delete(UserFollow).where(
            UserFollow.user_id == user_id,
            UserFollow.follow_type == follow_type,
            UserFollow.target_id == target_id,
        )
    )
    db.commit()


def is_following(db: Session, *, user_id: int, follow_type: str, target_id: int) -> bool:
    return (
        db.scalar(
            select(UserFollow.id).where(
                UserFollow.user_id == user_id,
                UserFollow.follow_type == follow_type,
                UserFollow.target_id == target_id,
            )
        )
        is not None
    )


def followed_target_ids(db: Session, *, user_id: int, follow_type: str) -> set[int]:
    return set(
        db.scalars(
            select(UserFollow.target_id).where(
                UserFollow.user_id == user_id, UserFollow.follow_type == follow_type
            )
        )
    )


def followers_of(db: Session, *, follow_type: str, target_id: int) -> list[int]:
    """User ids following a given city/category/source — the recipient set for
    a new-event alert. Returns ids only; the caller joins to preferences."""
    return list(
        db.scalars(
            select(UserFollow.user_id).where(
                UserFollow.follow_type == follow_type, UserFollow.target_id == target_id
            )
        )
    )


# --- alert preferences -------------------------------------------------------


def get_or_create_preferences(db: Session, *, user_id: int) -> AlertPreference:
    prefs = db.scalar(select(AlertPreference).where(AlertPreference.user_id == user_id))
    if prefs is not None:
        return prefs
    prefs = AlertPreference(user_id=user_id, unsubscribe_token=secrets.token_urlsafe(32))
    db.add(prefs)
    db.commit()
    db.refresh(prefs)
    return prefs


def update_preferences(db: Session, *, user_id: int, values: dict) -> AlertPreference:
    prefs = get_or_create_preferences(db, user_id=user_id)
    for key in (
        "in_app_enabled", "email_enabled", "notify_new_events",
        "notify_reminders", "notify_updates",
    ):
        if key in values:
            setattr(prefs, key, bool(values[key]))
    if values.get("frequency") in ALERT_FREQUENCIES:
        prefs.frequency = values["frequency"]
    db.commit()
    db.refresh(prefs)
    return prefs


def get_preferences_by_unsubscribe_token(db: Session, token: str) -> AlertPreference | None:
    if not token:
        return None
    return db.scalar(select(AlertPreference).where(AlertPreference.unsubscribe_token == token))


def unsubscribe_email(db: Session, token: str) -> bool:
    """One-click email unsubscribe (no login). Disables email delivery only;
    in-app alerts are untouched. Returns True if a matching user was found."""
    prefs = get_preferences_by_unsubscribe_token(db, token)
    if prefs is None:
        return False
    prefs.email_enabled = False
    db.commit()
    return True
