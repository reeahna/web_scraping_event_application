"""OAuth login orchestration (Phase 14).

Turns a provider round-trip into a local user, enforcing the security rules the
spec requires: server-side `state` validation (one-time, expiring), OIDC nonce
carried through, disabled-user enforcement, duplicate-identity protection (via
the unique (provider, subject) constraint), safe account linking (only on a
provider-verified email), unverified-email handling (never links to an existing
account), and a local-only redirect allowlist. Session-fixation prevention is
inherent: the caller issues a brand-new session token on success.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.email import normalize_email
from app.core.permissions import REGISTERED_USER
from app.models.external_identity import ExternalIdentity, OAuthLoginState
from app.models.role import Role
from app.models.user import User
from app.models.user_role import UserRole
from app.repositories.user import create_user, get_user_by_email
from app.services.oauth import (
    ExternalIdentityInfo,
    OAuthProvider,
    build_provider,
    enabled_provider_names,
    is_provider_enabled,
)

STATE_TTL_SECONDS = 600


def is_enabled(settings, provider_name: str) -> bool:
    return is_provider_enabled(settings, provider_name)


def enabled_providers(settings) -> list[str]:
    return enabled_provider_names(settings)


class OAuthError(Exception):
    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


def safe_next(next_url: str | None) -> str:
    """Redirect allowlist: only local paths, never an absolute/off-site URL."""
    if next_url and next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/"


def _redirect_uri(settings, provider_name: str) -> str:
    base = settings.oauth_redirect_base_url.rstrip("/")
    return f"{base}/auth/oauth/{provider_name}/callback"


def start_login(
    db: Session,
    settings,
    provider_name: str,
    *,
    next_url: str | None = None,
    provider: OAuthProvider | None = None,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(UTC)
    provider = provider or build_provider(settings, provider_name)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(16)
    db.add(
        OAuthLoginState(
            state=state, provider=provider_name, nonce=nonce,
            next_url=safe_next(next_url), created_at=now,
        )
    )
    db.commit()
    return provider.authorization_url(
        state=state, nonce=nonce, redirect_uri=_redirect_uri(settings, provider_name)
    )


def _consume_state(db: Session, provider_name: str, state: str, now: datetime) -> OAuthLoginState:
    record = db.scalar(select(OAuthLoginState).where(OAuthLoginState.state == state))
    if record is None or record.provider != provider_name:
        raise OAuthError("invalid_state", "OAuth state is missing or does not match.")
    created = record.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    expired = (now - created).total_seconds() > STATE_TTL_SECONDS
    # One-time use: always delete, even when expired.
    next_url = record.next_url
    nonce = record.nonce
    db.delete(record)
    db.commit()
    if expired:
        raise OAuthError("expired_state", "OAuth state has expired; please try again.")
    # Return a lightweight snapshot (the row is now gone).
    snapshot = OAuthLoginState(state=state, provider=provider_name, nonce=nonce, next_url=next_url)
    return snapshot


def complete_login(
    db: Session,
    settings,
    provider_name: str,
    *,
    code: str,
    state: str,
    provider: OAuthProvider | None = None,
    now: datetime | None = None,
) -> tuple[User, str]:
    now = now or datetime.now(UTC)
    snapshot = _consume_state(db, provider_name, state, now)
    provider = provider or build_provider(settings, provider_name)
    info = provider.fetch_identity(
        code=code, state=state, nonce=snapshot.nonce,
        redirect_uri=_redirect_uri(settings, provider_name),
    )
    user = _resolve_user(db, info, now)
    return user, safe_next(snapshot.next_url)


def _apply_identity_fields(identity: ExternalIdentity, info: ExternalIdentityInfo, now) -> None:
    identity.email = info.email
    identity.email_verified = info.email_verified
    identity.display_name = info.display_name
    identity.avatar_url = info.avatar_url
    identity.last_login_at = now


def _resolve_user(db: Session, info: ExternalIdentityInfo, now: datetime) -> User:
    identity = db.scalar(
        select(ExternalIdentity).where(
            ExternalIdentity.provider == info.provider,
            ExternalIdentity.subject == info.subject,
        )
    )
    if identity is not None:
        user = db.get(User, identity.user_id)
        if user is None or not user.is_active:
            raise OAuthError("account_disabled", "This account is disabled.")
        _apply_identity_fields(identity, info, now)
        db.commit()
        return user

    email_norm = normalize_email(info.email) if info.email else None
    if not email_norm:
        # No email at all — we cannot key a local account.
        raise OAuthError("email_required", "The provider did not supply an email address.")

    existing = get_user_by_email(db, email_norm)
    if existing is not None:
        # Safe account linking: only on a provider-VERIFIED email. An
        # unverified email must never take over an existing account.
        if not info.email_verified:
            raise OAuthError(
                "email_unverified_conflict",
                "That email already has an account. Sign in and link it from your account.",
            )
        if not existing.is_active:
            raise OAuthError("account_disabled", "This account is disabled.")
        return _link_identity(db, existing, info, now)

    # New account for a brand-new external identity.
    user = create_user(db, email=email_norm, hashed_password=None, full_name=info.display_name)
    role = db.scalar(select(Role).where(Role.name == REGISTERED_USER, Role.is_active.is_(True)))
    if role is not None:
        db.add(UserRole(user_id=user.id, role_id=role.id))
    _link_identity(db, user, info, now, commit=False)
    db.commit()
    db.refresh(user)
    return user


def _link_identity(
    db: Session, user: User, info: ExternalIdentityInfo, now: datetime, *, commit: bool = True
) -> User:
    identity = ExternalIdentity(
        user_id=user.id, provider=info.provider, subject=info.subject,
    )
    _apply_identity_fields(identity, info, now)
    db.add(identity)
    if commit:
        db.commit()
    return user
