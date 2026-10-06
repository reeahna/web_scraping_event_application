from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import utcnow

PASSWORD_RESET = "password_reset"
EMAIL_VERIFICATION = "email_verification"


class UserToken(Base):
    """A one-time link sent by email (password reset, address verification).

    Like UserSession, the primary key is a SHA-256 hash of the raw token, which
    only ever exists in the emailed link, so a database copy cannot be used to
    reset anyone's password."""

    __tablename__ = "user_tokens"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
