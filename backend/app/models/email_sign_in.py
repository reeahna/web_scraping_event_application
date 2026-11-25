"""Email-code sign-in and the sign-in history.

`EmailSignInCode` is one 6-digit code sent to an address. Only a salted hash of
the code is stored. A code works once, for 10 minutes, and only while it is the
newest code for its address: asking for another code invalidates every earlier
one. Wrong guesses are counted on the code, and a code that has used up its
guesses stops working.

`SignInHistory` is the per-person sign-in record ("customer history"): codes
sent, codes rejected and every successful sign-in, by any method (email code or
a social provider). It is deleted with the account.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import utcnow


class EmailSignInCode(Base):
    __tablename__ = "email_sign_in_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Normalized (app.core.email.normalize_email).
    email: Mapped[str] = mapped_column(String(255), index=True)
    # "<salt>$<sha256 hex>"; never the code itself.
    code_hash: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    # Set when the code signed someone in.
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Set when a newer code replaced it, its guesses ran out, or the email
    # could not be sent.
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    requested_ip: Mapped[str | None] = mapped_column(String(64), default=None)


class SignInHistory(Base):
    __tablename__ = "sign_in_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    # None until the address belongs to an account (a code sent to a new
    # address, a wrong code).
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, default=None
    )
    email: Mapped[str | None] = mapped_column(String(255), index=True, default=None)
    # "email_code", or the provider name ("google", "microsoft", ...), or "test".
    method: Mapped[str] = mapped_column(String(32))
    # "code_sent", "code_send_failed", "code_rejected", "signed_in", "refused".
    outcome: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str | None] = mapped_column(String(255), default=None)
    ip_address: Mapped[str | None] = mapped_column(String(64), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(255), default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
