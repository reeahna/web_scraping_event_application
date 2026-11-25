from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import utcnow

if TYPE_CHECKING:
    from app.models.user import User


class Suggestion(Base):
    """Something a visitor asked for: a new town (with the
    university it is home to), a new feature or a bug fix. Admins triage these
    in /admin/suggestions; the person sees the status of their own on /suggest
    (towns) and /feedback (features and bugs).

    `kind` is one of app.services.suggestions.SUGGESTION_KINDS and `status` one
    of SUGGESTION_STATUSES — validated there, never an arbitrary string write.
    """

    __tablename__ = "suggestions"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Kept when the account is deleted (the idea is still useful), but no
    # longer tied to anyone. Also empty for feature ideas and bug reports
    # sent from /feedback by someone who is not signed in.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, default=None
    )
    kind: Mapped[str] = mapped_column(String(16), index=True)
    # Town suggestions: the town and its state/region and university.
    # Feature suggestions and bug reports: a one-line summary in `title`.
    title: Mapped[str] = mapped_column(String(200))
    region: Mapped[str | None] = mapped_column(String(100), default=None)
    university: Mapped[str | None] = mapped_column(String(200), default=None)
    details: Mapped[str | None] = mapped_column(Text, default=None)
    # Optional reply address left by a visitor who was not signed in.
    contact_email: Mapped[str | None] = mapped_column(String(320), default=None)
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)
    admin_notes: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped["User | None"] = relationship()


class AnonymousFeedbackDay(Base):
    """How many feature ideas and bug reports visitors who were not signed in
    sent on one UTC day: the counter behind the daily ceiling on anonymous
    feedback (app.services.suggestions.ANONYMOUS_DAILY_LIMIT).

    One row per day. A message claims a slot with a single conditional UPDATE
    (count = count + 1 WHERE count < limit) in the same transaction as the
    message itself, so concurrent requests on any worker can never push the
    count past the limit, and a message that fails to save gives its slot back.
    """

    __tablename__ = "anonymous_feedback_days"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
