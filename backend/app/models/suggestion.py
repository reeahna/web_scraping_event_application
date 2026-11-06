from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import utcnow

if TYPE_CHECKING:
    from app.models.user import User


class Suggestion(Base):
    """Something a signed-in visitor asked for: a new town (with the
    university it is home to) or a new feature. Admins triage these in
    /admin/suggestions; the person sees the status of their own on /suggest.

    `kind` is one of app.services.suggestions.SUGGESTION_KINDS and `status` one
    of SUGGESTION_STATUSES — validated there, never an arbitrary string write.
    """

    __tablename__ = "suggestions"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Kept when the account is deleted (the idea is still useful), but no
    # longer tied to anyone.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, default=None
    )
    kind: Mapped[str] = mapped_column(String(16), index=True)
    # Town suggestions: the town and its state/region and university.
    # Feature suggestions: a one-line summary in `title`.
    title: Mapped[str] = mapped_column(String(200))
    region: Mapped[str | None] = mapped_column(String(100), default=None)
    university: Mapped[str | None] = mapped_column(String(200), default=None)
    details: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)
    admin_notes: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped["User | None"] = relationship()
