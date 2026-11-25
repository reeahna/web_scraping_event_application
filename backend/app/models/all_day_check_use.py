from datetime import datetime

from sqlalchemy import DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import utcnow


class AllDayCheckUse(Base):
    """One row per all-day backfill Check that has been applied.

    The primary key is the Check's audit entry id, so the database itself
    refuses a second use of the same Check on SQLite and PostgreSQL alike.
    Apply inserts this row first, in the same transaction as the event
    changes and its audit entry: either all of them commit or none do.
    """

    __tablename__ = "all_day_check_uses"

    check_id: Mapped[int] = mapped_column(ForeignKey("audit_logs.id"), primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
