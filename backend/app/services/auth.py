import hmac
import logging
from datetime import UTC, datetime, timedelta

from fastapi import Request
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.email import normalize_email
from app.core.exceptions import NotAuthenticatedError
from app.core.security import generate_session_token, hash_session_token
from app.models.base import as_aware_utc
from app.models.user import User
from app.models.user_session import UserSession

logger = logging.getLogger(__name__)

# A shorter configured key is treated as unset rather than accepted.
ADMIN_API_KEY_MIN_LENGTH = 32


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


def _bearer_token(request: Request) -> str | None:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def _user_for_admin_api_key(db: Session, supplied: str) -> User:
    """The account an admin access key acts as. Raises NotAuthenticatedError
    for a wrong key, or when the key or its account is not configured."""
    settings = get_settings()
    key = settings.admin_api_key or ""
    email = settings.admin_api_key_user_email or ""
    if len(key) < ADMIN_API_KEY_MIN_LENGTH or not email:
        raise NotAuthenticatedError("Not authenticated")
    if not hmac.compare_digest(supplied.encode(), key.encode()):
        logger.warning("admin access key rejected: wrong key")
        raise NotAuthenticatedError("Not authenticated")
    user = db.query(User).filter(User.email == normalize_email(email)).first()
    if user is None or not user.is_active:
        logger.warning("admin access key rejected: account missing or disabled")
        raise NotAuthenticatedError("Not authenticated")
    return user


def resolve_current_user(request: Request, db: Session, cookie_name: str) -> User:
    supplied = _bearer_token(request)
    if supplied is not None:
        user = _user_for_admin_api_key(db, supplied)
        request.state.authenticated_by_api_key = True
        return user

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
    # Lets the visitor log (app.services.site_visits) show who was signed in.
    request.state.visit_user_id = user.id
    return user
