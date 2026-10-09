"""External identities and OAuth login state (Phase 14).

`ExternalIdentity` is the provider-independent link between a local user and an
account at an external provider (Google/Microsoft/Facebook or any future one).
The unique (provider, subject) constraint is the duplicate-identity guard: one
provider account maps to at most one local user. We deliberately store no
third-party password and do not retain provider access/refresh tokens — only
the stable subject, the (optionally verified) email, and display fields.

`OAuthLoginState` is the short-lived server-side record of an in-flight login,
holding the CSRF `state` and OIDC `nonce` so the callback can validate them
without relying on session middleware.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin


class ExternalIdentity(Base, TimestampMixin):
    __tablename__ = "external_identities"
    __table_args__ = (
        UniqueConstraint("provider", "subject", name="uq_external_identity_provider_subject"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32), index=True)
    subject: Mapped[str] = mapped_column(String(255), index=True)
    email: Mapped[str | None] = mapped_column(String(255), default=None)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    display_name: Mapped[str | None] = mapped_column(String(255), default=None)
    avatar_url: Mapped[str | None] = mapped_column(String(2000), default=None)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class OAuthLoginState(Base):
    __tablename__ = "oauth_login_states"

    id: Mapped[int] = mapped_column(primary_key=True)
    state: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(32))
    nonce: Mapped[str | None] = mapped_column(String(64), default=None)
    next_url: Mapped[str | None] = mapped_column(String(500), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
