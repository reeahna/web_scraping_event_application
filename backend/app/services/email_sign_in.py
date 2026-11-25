"""Signing in with a code sent by email.

1. `prepare_code`: the person types an address. Every earlier code for it is
   invalidated and a new random 6-digit code is stored, valid for 10 minutes.
   Only an HMAC of the code is stored, keyed with EMAIL_CODE_SECRET, which is
   not in the database. `deliver_code` then emails it; the router runs that
   after the response, so the reply takes the same time and looks the same
   whatever happens to the address.
2. `verify_code`: the person types the code. Only the newest code for the
   address can match. A wrong guess is counted on the code with an atomic
   UPDATE; after EMAIL_CODE_MAX_ATTEMPTS it stops working. A right one is
   claimed with one conditional UPDATE (unused, not invalidated, not expired,
   guesses left) that must change exactly one row, so two requests racing with
   the same code cannot both sign in.

Limits (codes per address and per IP address per hour, wrong codes per IP
address per 15 minutes) are counted from the database, so they hold across
web workers, instances and restarts.

The person sees the same thing whether or not the address has an account,
whether it is disabled, and whether it has hit its hourly limit, so the form
cannot be used to find out who has an account.

A correct code proves the person reads that inbox, so it counts as a verified
address: it marks the account's email verified, unlinks any social sign-in
that never verified the address (as a verifying provider does), and makes
SUPERADMIN_EMAIL a Super Administrator.
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

logger = logging.getLogger("app.email_sign_in")

METHOD = "email_code"
CODE_LENGTH = 6
CODE_TTL = timedelta(minutes=10)
# Old codes are deleted once they are this old; none can still work.
CODE_RETENTION = timedelta(days=1)
HOUR = timedelta(hours=1)
QUARTER_HOUR = timedelta(minutes=15)
# History outcomes written once per accepted request, whatever happens next;
# the per-IP request limit counts these.
REQUEST_OUTCOMES = ("code_issued", "refused")

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
class PendingCode:
    """A code stored and waiting to be emailed."""

    code_id: int
    email: str
    code: str
    user_id: int | None


@dataclass(frozen=True)
class RequestResult:
    email: str
    # None when nothing is to be sent (disabled account, new accounts closed,
    # hourly limit for the address). The person is told the same either way.
    pending: PendingCode | None


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


def hash_code(secret: str, email: str, code: str) -> str:
    """HMAC-SHA256 of the code, keyed with the server secret. Without the
    secret, a copy of the database gives no way to test candidate codes."""
    digest = hmac.new(secret.encode(), f"{email}:{code}".encode(), hashlib.sha256)
    return f"v1${digest.hexdigest()}"


def code_matches(secret: str, stored: str, email: str, code: str) -> bool:
    return hmac.compare_digest(stored, hash_code(secret, email, code))


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


def _count_history(
    db: Session, *, ip_address: str, outcomes: tuple[str, ...], since: datetime
) -> int:
    return db.scalar(
        select(func.count(SignInHistory.id)).where(
            SignInHistory.method == METHOD,
            SignInHistory.ip_address == ip_address[:64],
            SignInHistory.outcome.in_(outcomes),
            SignInHistory.created_at > since,
        )
    ) or 0


def prepare_code(
    db: Session,
    settings,
    raw_email: str | None,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
    now: datetime | None = None,
) -> RequestResult:
    """Store a new code for the address (invalidating older ones) when it may
    have one. Raises EmailSignInError only for a malformed address or for the
    per-IP limit, neither of which depends on the address having an account."""
    now = now or datetime.now(UTC)
    email = clean_email(raw_email)

    if ip_address and _count_history(
        db, ip_address=ip_address, outcomes=REQUEST_OUTCOMES, since=now - HOUR
    ) >= settings.email_codes_per_ip_per_hour:
        raise EmailSignInError(
            "too_many_codes",
            "Too many codes have been requested. Wait a while, then try again.",
            status_code=429,
        )

    # Housekeeping: no code this old can still work, and history is kept only
    # for its retention period.
    db.execute(delete(EmailSignInCode).where(EmailSignInCode.created_at < now - CODE_RETENTION))
    db.execute(
        delete(SignInHistory).where(
            SignInHistory.created_at
            < now - timedelta(days=settings.sign_in_history_retention_days)
        )
    )

    existing = get_user_by_email(db, email)
    user_id = existing.id if existing else None
    refusal = None
    if existing is not None and not existing.is_active:
        refusal = "account disabled"
    elif not _registration_allows(settings, email, existing):
        refusal = "registration closed"
    else:
        recent = db.scalar(
            select(func.count(EmailSignInCode.id)).where(
                EmailSignInCode.email == email,
                EmailSignInCode.created_at > now - HOUR,
            )
        )
        if (recent or 0) >= settings.email_codes_per_address_per_hour:
            refusal = "hourly limit for the address"
    if refusal:
        # Send nothing, and answer exactly as if a code had gone out.
        record_history(
            db, method=METHOD, outcome="refused", email=email, user_id=user_id,
            detail=refusal, ip_address=ip_address, user_agent=user_agent,
        )
        db.commit()
        return RequestResult(email=email, pending=None)

    # Only the newest code works.
    db.execute(
        update(EmailSignInCode)
        .where(
            EmailSignInCode.email == email,
            EmailSignInCode.used_at.is_(None),
            EmailSignInCode.invalidated_at.is_(None),
        )
        .values(invalidated_at=now)
        .execution_options(synchronize_session=False)
    )
    code = generate_code()
    row = EmailSignInCode(
        email=email,
        code_hash=hash_code(settings.email_code_secret, email, code),
        created_at=now,
        expires_at=now + CODE_TTL,
        failed_attempts=0,
        requested_ip=ip_address[:64] if ip_address else None,
    )
    db.add(row)
    record_history(
        db, method=METHOD, outcome="code_issued", email=email, user_id=user_id,
        ip_address=ip_address, user_agent=user_agent,
    )
    db.commit()
    return RequestResult(
        email=email,
        pending=PendingCode(code_id=row.id, email=email, code=code, user_id=user_id),
    )


def deliver_code(
    db: Session,
    settings,
    sender: EmailSender,
    pending: PendingCode,
    *,
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> bool:
    """Email a prepared code. On failure the code is invalidated and the
    failure recorded; the person can ask for a new one from the code page."""
    sent = sender.send(
        EmailMessage(
            to=pending.email,
            subject=f"{pending.code} is your {settings.app_name} sign-in code",
            body=_email_body(settings, pending.code),
        )
    )
    if not sent:
        db.execute(
            update(EmailSignInCode)
            .where(EmailSignInCode.id == pending.code_id)
            .values(invalidated_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        record_history(
            db, method=METHOD, outcome="code_send_failed", email=pending.email,
            user_id=pending.user_id, ip_address=ip_address, user_agent=user_agent,
        )
        db.commit()
        logger.warning("Sign-in code email could not be sent (code id %s)", pending.code_id)
    return sent


def claim_code(db: Session, code_id: int, now: datetime, max_attempts: int) -> bool:
    """Mark a code used, only if it is still unused, valid, unexpired and has
    guesses left. One conditional UPDATE: of two racing claims exactly one
    changes the row. Commits."""
    result = db.execute(
        update(EmailSignInCode)
        .where(
            EmailSignInCode.id == code_id,
            EmailSignInCode.used_at.is_(None),
            EmailSignInCode.invalidated_at.is_(None),
            EmailSignInCode.expires_at > now,
            EmailSignInCode.failed_attempts < max_attempts,
        )
        .values(used_at=now)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def count_wrong_guess(db: Session, code_id: int, now: datetime, max_attempts: int) -> int:
    """Add one wrong guess to a code atomically (no lost updates when guesses
    race), invalidate it once it has run out, and return its guess count.
    Commits."""
    db.execute(
        update(EmailSignInCode)
        .where(
            EmailSignInCode.id == code_id,
            EmailSignInCode.used_at.is_(None),
            EmailSignInCode.invalidated_at.is_(None),
        )
        .values(failed_attempts=EmailSignInCode.failed_attempts + 1)
        .execution_options(synchronize_session=False)
    )
    db.execute(
        update(EmailSignInCode)
        .where(
            EmailSignInCode.id == code_id,
            EmailSignInCode.invalidated_at.is_(None),
            EmailSignInCode.failed_attempts >= max_attempts,
        )
        .values(invalidated_at=now)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return db.scalar(
        select(EmailSignInCode.failed_attempts).where(EmailSignInCode.id == code_id)
    ) or 0


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
    if ip_address and _count_history(
        db, ip_address=ip_address, outcomes=("code_rejected",), since=now - QUARTER_HOUR
    ) >= settings.email_code_failures_per_ip_per_15_minutes:
        raise EmailSignInError(
            "too_many_checks",
            "Too many codes tried. Wait a few minutes, then try again.",
            status_code=429,
        )

    existing = get_user_by_email(db, email)
    user_id = existing.id if existing else None
    max_attempts = settings.email_code_max_attempts
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
    code_id = row.id

    if not code_matches(settings.email_code_secret, row.code_hash, email, code):
        attempts = count_wrong_guess(db, code_id, now, max_attempts)
        left = max_attempts - attempts
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

    # Used up before anything else, atomically, so it can never sign in twice
    # (another request may have claimed it since it was read above).
    if not claim_code(db, code_id, now, max_attempts):
        _reject(db, email, user_id, "code already used", ip_address, user_agent)
        raise EmailSignInError("expired", _EXPIRED)

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
