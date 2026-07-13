from datetime import UTC, datetime, timedelta

from fastapi import Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.exceptions import NotAuthenticatedError
from app.core.security import generate_session_token, hash_session_token, verify_password
from app.models.base import as_aware_utc
from app.models.user import User
from app.models.user_session import UserSession


def create_session(db: Session, user: User, request: Request | None = None) -> str:
    """Create a session record and return the raw token (only ever sent to the
    client via cookie — the DB stores just its hash, see UserSession)."""
    settings = get_settings()
    raw_token = generate_session_token()
    now = datetime.now(UTC)
    session = UserSession(
        id=hash_session_token(raw_token),
        user_id=user.id,
        created_at=now,
        expires_at=now + timedelta(seconds=settings.session_ttl_seconds),
        last_seen_at=now,
        ip_address=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
    )
    db.add(session)
    db.commit()
    return raw_token


def get_session_by_token(db: Session, raw_token: str) -> UserSession | None:
    session = db.get(UserSession, hash_session_token(raw_token))
    if session is None:
        return None
    if as_aware_utc(session.expires_at) < datetime.now(UTC):
        db.delete(session)
        db.commit()
        return None
    return session


def delete_session(db: Session, raw_token: str) -> None:
    session = db.get(UserSession, hash_session_token(raw_token))
    if session is not None:
        db.delete(session)
        db.commit()


def authenticate_local_user(db: Session, email: str, password: str) -> User | None:
    """Verify email/password only. Deliberately does not check is_active — callers
    check that separately so failure reasons (bad credentials vs. disabled
    account) can be audited distinctly."""
    user = db.query(User).filter(User.email == email).first()
    if user is None or not user.hashed_password:
        return None
    if not verify_password(password, user.hashed_password):
        return None
    return user


def resolve_current_user(request: Request, db: Session, cookie_name: str) -> User:
    token = request.cookies.get(cookie_name)
    if not token:
        raise NotAuthenticatedError("Not authenticated")

    session = get_session_by_token(db, token)
    if session is None:
        raise NotAuthenticatedError("Session expired or invalid")

    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        # Disabled/deleted account: kill the session so it can't be reused.
        db.delete(session)
        db.commit()
        raise NotAuthenticatedError("Account is disabled")

    session.last_seen_at = datetime.now(UTC)
    db.commit()
    return user
