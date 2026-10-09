from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import utcnow

if TYPE_CHECKING:
    from app.models.user import User


class Notification(Base):
    """One row per recipient user (fan-out at creation time, not a single
    shared role-audience row) — this is what lets mark-read/dismiss be
    per-user without a second join table, matching how AuditLog is already
    per-actor in this codebase. See app.services.notifications.

    `delivery_status`/`delivery_attempts`/`provider` reflect the outcome of
    the one delivery channel this row attempted beyond the in-app write
    (currently email; "not_applicable" when no such channel was configured).
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    recipient_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    notification_type: Mapped[str] = mapped_column(String(64), index=True)
    severity: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(255))
    message: Mapped[str] = mapped_column(String(1000))
    related_resource_type: Mapped[str | None] = mapped_column(String(32), default=None)
    related_resource_id: Mapped[int | None] = mapped_column(Integer, default=None)
    action_url: Mapped[str | None] = mapped_column(String(500), default=None)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    delivery_status: Mapped[str] = mapped_column(String(16), default="not_applicable")
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    provider: Mapped[str | None] = mapped_column(String(32), default=None)
    dedup_fingerprint: Mapped[str] = mapped_column(String(64), index=True)

    recipient: Mapped["User"] = relationship()
