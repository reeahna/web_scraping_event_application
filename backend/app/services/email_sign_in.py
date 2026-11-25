"""Signing in with a code sent by email.

1. `request_code`: the person types an address. Every earlier code for it is
   invalidated, a new random 6-digit code is stored (salted hash only, valid
   for 10 minutes) and emailed. Limits: codes per address per hour and per IP
   address per hour.
2. `verify_code`: the person types the code. Only the newest code for the
   address can match. A wrong guess is counted on the code; after
   EMAIL_CODE_MAX_ATTEMPTS it stops working. A right one is used up and
   signs the person in (creating the account if new accounts are open).

A correct code proves the person reads that inbox, so it counts as a verified
address: it marks the account's email verified, unlinks any social sign-in
that never verified the address (as a verifying provider does), and makes
SUPERADMIN_EMAIL a Super Administrator.

The person is told the same thing whether or not the address has an account,
so the form cannot be used to find out who has one.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.core.email import normalize_email
from app.core.permissions import REGISTERED_USER
from app.models.base import as_aware_utc
from app.models.email_sign_in import EmailSignInCode, SignInHistory
from app.models.role import Role
from app.models.user import User
from app.models.user_role import UserRole
from app.repositories.user import create_user, get_user_by_email
from app.services.email import EmailMessage, EmailSender
from app.services.oauth_login import evict_unverified_identities, grant_superadmin
from app.services.rate_limit import get_rate_limit_backend

logger = logging.getLogger("app.email_sign_in")

METHOD = "email_code"
CODE_LENGTH = 6
CODE_TTL = timedelta(minutes=10)
# Old codes are deleted once they are this old; none can still work.
CODE_RETENTION = timedelta(days=1)
HOUR = 3600
QUARTER_HOUR = 15 * 60

# Deliberately loose: one @, no spaces, a dot in the domain. The real check is
# whether the person can read the code we send.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class EmailSignInError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class RequestResult:
    email: str
    # False when nothing was sent on purpose (new accounts closed and no
    # account for the address). The person is told the same either way.
    sent: bool


def clean_email(raw: str | None) -> str:
    email = normalize_email(raw or "")
    if not email or len(email) > 254 or not _EMAIL_RE.match(email):
        raise EmailSignInError("invalid_email", "Enter a valid email address.")
    return email


def clean_code(raw: str | None) -> str:
    """The digits the person typed, allowing spaces or a dash between them."""
    code = re.sub(r"[\s-]", "", raw or "")
    if len(code) != CODE_LENGTH or not code.isdigit():
        raise EmailSignInError(
            "invalid_code", f"Enter the {CODE_LENGTH}-digit code from the email."
        )
    return code


def generate_code() -> str:
    return f"{secrets.randbelow(10**CODE_LENGTH):0{CODE_LENGTH}d}"


def _digest(salt: str, email: str, code: str) -> str:
    return hashlib.sha256(f"{salt}:{email}:{code}".encode()).hexdigest()


def hash_code(email: str, code: str) -> str:
    salt = secrets.token_hex(16)
    return f"{salt}${_digest(salt, email, code)}"


def code_matches(stored: str, email: str, code: str) -> bool:
    salt, _, digest = stored.partition("$")
    return bool(digest) and hmac.compare_digest(digest, _digest(salt, email, code))


def record_history(
    db: Session,
    *,
    method: str,
    outcome: str,
    email: str | None = None,
    user_id: int | None = None,
    detail: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Add a sign-in history row. The caller commits."""
    db.add(
        SignInHistory(
            user_id=user_id,
            email=email,
            method=method[:32],
            outcome=outcome[:32],
            detail=detail[:255] if detail else None,
            ip_address=ip_address[:64] if ip_address else None,
            user_agent=user_agent[:255] if user_agent else None,
        )
    )


def _registration_allows(settings, email: str, existing: User | None) -> bool:
    if existing is not None:
        return True
    if getattr(settings, "registration_enabled", True):
        return True
    configured = getattr(settings, "superadmin_email", None)
    return bool(configured and normalize_email(configured) == email)


def _within_ip_limit(key: str, ip_address: str | None, *, limit: int, window: int) -> bool:
    if not ip_address:
        return True
    return get_rate_limit_backend().allow(
        f"{key}:{ip_address}", limit=limit, window_seconds=window
    )


def _email_body(settings, code: str) -> str:
    minutes = int(CODE_TTL.total_seconds() // 60)
    return (
        f"Your {settings.app_name} sign-in code is {code}\n\n"
        f"Enter it on the sign-in page within {minutes} minutes. It works once, "
        "and only the newest code we sent works.\n\n"
        "If you didn't ask to sign in, you can ignore this email. Nobody can "
        "sign in without the code.\n\n"
        f"{settings.public_base_url}\n"
    )


def request_code(
    db: Session,
    settings,
    sender: EmailSender,
    raw_email: str | None,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> RequestResult:
    now = now or datetime.now(UTC)
    email = clean_email(raw_email)

    recent = db.scalar(
        select(func.count(EmailSignInCode.id)).where(
            EmailSignInCode.email == email,
            EmailSignInCode.created_at > now - timedelta(seconds=HOUR),
        )
    )
    if (recent or 0) >= settings.email_codes_per_address_per_hour or not _within_ip_limit(
        "email-code-send", ip_address,
        limit=settings.email_codes_per_ip_per_hour, window=HOUR,
    ):
        raise EmailSignInError(
            "too_many_codes",
            "Too many codes have been requested. Wait a while, then try again.",
            status_code=429,
        )

    # Housekeeping: no code this old can still work.
    db.execute(delete(EmailSignInCode).where(EmailSignInCode.created_at < now - CODE_RETENTION))

    existing = get_user_by_email(db, email)
    if existing is not None and not existing.is_active:
        existing_ok = False
    else:
        existing_ok = _registration_allows(settings, email, existing)
    if not existing_ok:
        # Disabled account, or new accounts closed: send nothing, say nothing
        # different.
        record_history(
            db, method=METHOD, outcome="refused", email=email,
            user_id=existing.id if existing else None,
            detail="account disabled" if existing else "registration closed",
            ip_address=ip_address, user_agent=user_agent,
        )
        db.commit()
        return RequestResult(email=email, sent=False)

    # Only the newest code works.
    db.execute(
        update(EmailSignInCode)
        .where(
            EmailSignInCode.email == email,
            EmailSignInCode.used_at.is_(None),
            EmailSignInCode.invalidated_at.is_(None),
        )
        .values(invalidated_at=now)
    )
    code = generate_code()
    row = EmailSignInCode(
        email=email,
        code_hash=hash_code(email, code),
        created_at=now,
        expires_at=now + CODE_TTL,
        failed_attempts=0,
        requested_ip=ip_address[:64] if ip_address else None,
    )
    db.add(row)
    db.commit()

    sent = sender.send(
        EmailMessage(
            to=email,
            subject=f"{code} is your {settings.app_name} sign-in code",
            body=_email_body(settings, code),
        )
    )
    user_id = existing.id if existing else None
    if not sent:
        row.invalidated_at = now
        record_history(
            db, method=METHOD, outcome="code_send_failed", email=email, user_id=user_id,
            ip_address=ip_address, user_agent=user_agent,
        )
        db.commit()
        raise EmailSignInError(
            "send_failed",
            "We couldn't send the email just now. Please try again in a few minutes.",
            status_code=503,
        )
    record_history(
        db, method=METHOD, outcome="code_sent", email=email, user_id=user_id,
        ip_address=ip_address, user_agent=user_agent,
    )
    db.commit()
    return RequestResult(email=email, sent=True)


def _reject(
    db: Session, email: str, user_id: int | None, detail: str, ip_address, user_agent
) -> None:
    record_history(
        db, method=METHOD, outcome="code_rejected", email=email, user_id=user_id,
        detail=detail, ip_address=ip_address, user_agent=user_agent,
    )
    db.commit()


_EXPIRED = (
    "That code has expired or is no longer valid. Request a new code; only the "
    "newest one works."
)


def verify_code(
    db: Session,
    settings,
    raw_email: str | None,
    raw_code: str | None,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> User:
    """The signed-in user for a correct code. Raises EmailSignInError otherwise."""
    now = now or datetime.now(UTC)
    email = clean_email(raw_email)
    code = clean_code(raw_code)
    if not _within_ip_limit(
        "email-code-check", ip_address,
        limit=settings.email_code_checks_per_ip_per_15_minutes, window=QUARTER_HOUR,
    ):
        raise EmailSignInError(
            "too_many_checks",
            "Too many codes tried. Wait a few minutes, then try again.",
            status_code=429,
        )

    existing = get_user_by_email(db, email)
    user_id = existing.id if existing else None
    row = db.scalar(
        select(EmailSignInCode)
        .where(
            EmailSignInCode.email == email,
            EmailSignInCode.used_at.is_(None),
            EmailSignInCode.invalidated_at.is_(None),
        )
        .order_by(EmailSignInCode.created_at.desc(), EmailSignInCode.id.desc())
        .limit(1)
    )
    if row is None or as_aware_utc(row.expires_at) <= now:
        _reject(db, email, user_id, "no valid code", ip_address, user_agent)
        raise EmailSignInError("expired", _EXPIRED)

    if not code_matches(row.code_hash, email, code):
        row.failed_attempts = (row.failed_attempts or 0) + 1
        left = settings.email_code_max_attempts - row.failed_attempts
        if left <= 0:
            row.invalidated_at = now
        _reject(db, email, user_id, "wrong code", ip_address, user_agent)
        if left <= 0:
            raise EmailSignInError(
                "too_many_attempts",
                "That code was entered wrong too many times and no longer works. "
                "Request a new code.",
            )
        raise EmailSignInError(
            "wrong_code",
            f"That code isn't right. {left} {'try' if left == 1 else 'tries'} left.",
        )

    # Used up before anything else, so it can never sign in twice.
    row.used_at = now
    db.commit()

    if existing is not None and not existing.is_active:
        _reject(db, email, user_id, "account disabled", ip_address, user_agent)
        raise EmailSignInError(
            "account_disabled", "This account has been disabled.", status_code=403
        )
    if not _registration_allows(settings, email, existing):
        _reject(db, email, None, "registration closed", ip_address, user_agent)
        raise EmailSignInError(
            "registration_closed", "New accounts aren't being accepted right now.",
            status_code=403,
        )

    user = existing
    if user is None:
        user = create_user(db, email=email)
        role = db.scalar(
            select(Role).where(Role.name == REGISTERED_USER, Role.is_active.is_(True))
        )
        if role is not None:
            db.add(UserRole(user_id=user.id, role_id=role.id))
    if user.email_verified_at is None:
        user.email_verified_at = now
    evict_unverified_identities(db, user)
    db.commit()
    db.refresh(user)

    configured = getattr(settings, "superadmin_email", None)
    if configured and normalize_email(configured) == email:
        grant_superadmin(db, user, detail="Signed in with a code sent to SUPERADMIN_EMAIL.")
    return user


def delete_for_account(db: Session, user_id: int, email: str) -> None:
    """Remove a deleted account's codes and history. The caller commits."""
    db.execute(delete(EmailSignInCode).where(EmailSignInCode.email == email))
    db.execute(
        delete(SignInHistory).where(
            (SignInHistory.user_id == user_id) | (SignInHistory.email == email)
        )
    )
