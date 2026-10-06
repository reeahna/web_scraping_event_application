"""One-time links sent by email: password reset and address verification.

Both only work once email delivery is configured (see
app.services.email.email_delivery_available); until then the routes say so and
the links that lead to them are not shown.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.email import normalize_email
from app.core.security import generate_session_token, hash_password, hash_session_token
from app.models.base import as_aware_utc
from app.models.user import User
from app.models.user_session import UserSession
from app.models.user_token import EMAIL_VERIFICATION, PASSWORD_RESET, UserToken
from app.services.email import EmailMessage, EmailSender, get_email_sender
from app.services.seo import absolute_url

# A reset link is a key to the account, so it is short-lived; a verification
# link only confirms an address, so it can wait a few days in an inbox.
TOKEN_LIFETIMES = {
    PASSWORD_RESET: timedelta(hours=1),
    EMAIL_VERIFICATION: timedelta(days=3),
}


def _issue(db: Session, user: User, purpose: str) -> str:
    """A new raw token for `user`. Any earlier unused one for the same purpose
    is withdrawn, so only the most recent link works."""
    db.query(UserToken).filter(
        UserToken.user_id == user.id,
        UserToken.purpose == purpose,
        UserToken.used_at.is_(None),
    ).delete(synchronize_session=False)
    raw = generate_session_token()
    now = datetime.now(UTC)
    db.add(
        UserToken(
            id=hash_session_token(raw),
            user_id=user.id,
            purpose=purpose,
            created_at=now,
            expires_at=now + TOKEN_LIFETIMES[purpose],
        )
    )
    db.commit()
    return raw


def find_valid_token(db: Session, raw: str, purpose: str) -> UserToken | None:
    """The unused, unexpired token for this raw value and purpose, or None."""
    if not raw:
        return None
    token = db.get(UserToken, hash_session_token(raw))
    if (
        token is None
        or token.purpose != purpose
        or token.used_at is not None
        or as_aware_utc(token.expires_at) < datetime.now(UTC)
    ):
        return None
    return token


def _send(sender: EmailSender | None, to: str, subject: str, body: str) -> bool:
    sender = sender or get_email_sender(get_settings())
    return sender.send(EmailMessage(to=to, subject=subject, body=body))


def request_password_reset(db: Session, email: str, *, sender: EmailSender | None = None) -> None:
    """Email a reset link if an active account has this address. Says nothing
    either way, so the form cannot be used to find out who has an account."""
    user = db.query(User).filter(User.email == normalize_email(email)).first()
    if user is None or not user.is_active:
        return
    raw = _issue(db, user, PASSWORD_RESET)
    site = get_settings().app_name
    link = absolute_url(f"/auth/reset-password?token={raw}")
    _send(
        sender,
        user.email,
        f"Reset your {site} password",
        f"Someone asked to reset the password for your {site} account.\n\n"
        f"To choose a new password, open this link within the next hour:\n{link}\n\n"
        "If this wasn't you, ignore this email; your password has not changed.",
    )


def reset_password(db: Session, token: UserToken, new_password: str) -> User:
    """Set the new password, use up the token, and sign the account out
    everywhere, since whoever knew the old password should not stay signed in.
    Following an emailed link also proves the address, so it is verified."""
    user = db.get(User, token.user_id)
    now = datetime.now(UTC)
    user.hashed_password = hash_password(new_password)
    user.email_verified_at = user.email_verified_at or now
    token.used_at = now
    db.query(UserSession).filter(UserSession.user_id == user.id).delete(
        synchronize_session=False
    )
    db.commit()
    return user


def send_verification_email(db: Session, user: User, *, sender: EmailSender | None = None) -> bool:
    if user.email_verified_at is not None:
        return False
    raw = _issue(db, user, EMAIL_VERIFICATION)
    site = get_settings().app_name
    link = absolute_url(f"/auth/verify-email?token={raw}")
    return _send(
        sender,
        user.email,
        f"Confirm your email for {site}",
        f"Welcome to {site}! Please confirm this is your email address:\n{link}\n\n"
        "If you didn't create an account, ignore this email.",
    )


def verify_email(db: Session, token: UserToken) -> User:
    user = db.get(User, token.user_id)
    now = datetime.now(UTC)
    user.email_verified_at = user.email_verified_at or now
    token.used_at = now
    db.commit()
    return user
