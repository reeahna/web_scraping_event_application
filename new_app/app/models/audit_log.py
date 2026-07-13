from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import utcnow

if TYPE_CHECKING:
    from app.models.user import User


class AuditLog(Base):
    """Append-only log of notable actions; no updated_at since entries are immutable."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    action: Mapped[str] = mapped_column(String(255))
    entity_type: Mapped[str | None] = mapped_column(String(100), default=None)
    entity_id: Mapped[int | None] = mapped_column(default=None)
    detail: Mapped[str | None] = mapped_column(Text, default=None)
    # JSON-encoded snapshots; never populated with passwords, session tokens, or secrets.
    before_state: Mapped[str | None] = mapped_column(Text, default=None)
    after_state: Mapped[str | None] = mapped_column(Text, default=None)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    ip_address: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped["User | None"] = relationship()
