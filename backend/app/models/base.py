from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_aware_utc(value: datetime) -> datetime:
    """SQLite's DateTime(timezone=True) doesn't actually preserve tzinfo — values
    read back are naive. Normalize before comparing against an aware datetime."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
