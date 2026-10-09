"""Registered-user engagement (Phase 13): saved events, follows, alert
preferences, and a delivery ledger.

Every row is owned by exactly one user, and every query in the services scopes
to the current user — one user's saved events, follows, or preferences are
never visible to another. Nothing here grants any administrative capability.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin

# Follow targets. A single polymorphic table keeps city/category/source follows
# uniform; `target_id` references the relevant table by convention (validated
# in the service, not by a DB FK, so a followed row's deletion never cascades
# into a user's follow set unexpectedly).
FOLLOW_CITY = "city"
FOLLOW_CATEGORY = "category"
FOLLOW_SOURCE = "source"

ALERT_FREQUENCIES = ("immediate", "daily", "weekly", "off")


class SavedEvent(Base, TimestampMixin):
    __tablename__ = "saved_events"
    __table_args__ = (UniqueConstraint("user_id", "event_id", name="uq_saved_event_user_event"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    event_id: Mapped[int] = mapped_column(
        ForeignKey("events.id", ondelete="CASCADE"), index=True
    )


class UserFollow(Base, TimestampMixin):
    __tablename__ = "user_follows"
    __table_args__ = (
        UniqueConstraint("user_id", "follow_type", "target_id", name="uq_user_follow"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    follow_type: Mapped[str] = mapped_column(String(16), index=True)
    target_id: Mapped[int] = mapped_column(Integer, index=True)


class AlertPreference(Base, TimestampMixin):
    __tablename__ = "alert_preferences"
    __table_args__ = (UniqueConstraint("user_id", name="uq_alert_preference_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    in_app_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Email is opt-in and, separately, the app only actually sends when an
    # email backend is configured (see app.services.email).
    email_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    frequency: Mapped[str] = mapped_column(String(16), default="immediate")
    notify_new_events: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_reminders: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_updates: Mapped[bool] = mapped_column(Boolean, default=True)
    # Opaque token for one-click email unsubscribe without logging in.
    unsubscribe_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    last_digest_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )


class AlertDelivery(Base, TimestampMixin):
    """Ledger of every alert queued/sent to a user, per channel. The unique
    (user, alert_key, channel) constraint is what prevents duplicates: an alert
    is recorded once and re-recording is a no-op, so a re-scrape or a retried
    digest never double-notifies."""

    __tablename__ = "alert_deliveries"
    __table_args__ = (
        UniqueConstraint("user_id", "alert_key", "channel", name="uq_alert_delivery"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    alert_key: Mapped[str] = mapped_column(String(200), index=True)
    channel: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="sent")
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"), default=None
    )
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(String(1000))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
