"""Visitor suggestions: new towns (with their university), new features and
bug reports.

Signed-in visitors suggest towns from /suggest. Anyone, signed in or not, can
send feature ideas and bug reports from /feedback ("New features / bug fixes");
admins holding reports.view see
them in /admin/suggestions and those holding reports.manage change their status.
"""

import re
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models.suggestion import AnonymousFeedbackDay, Suggestion
from app.models.user import User
from app.services.notifications import build_dedup_fingerprint, notify
from app.services.rbac import users_with_permission

KIND_TOWN = "town"
KIND_FEATURE = "feature"
KIND_BUG = "bug"
SUGGESTION_KINDS: tuple[str, ...] = (KIND_TOWN, KIND_FEATURE, KIND_BUG)
# The kinds sent from the "New features / bug fixes" page.
FEEDBACK_KINDS: tuple[str, ...] = (KIND_FEATURE, KIND_BUG)

KIND_LABELS: dict[str, str] = {
    KIND_TOWN: "Town",
    KIND_FEATURE: "Feature",
    KIND_BUG: "Bug",
}

SUGGESTION_STATUSES: dict[str, str] = {
    "new": "New",
    "planned": "Planned",
    "done": "Added",
    "declined": "Not planned",
}


def status_label(kind: str, status: str) -> str:
    """The status as the person reads it: a bug that was dealt with is
    "Fixed", not "Added"."""
    if kind == KIND_BUG and status == "done":
        return "Fixed"
    return SUGGESTION_STATUSES.get(status, status)

TITLE_MAX = 200
REGION_MAX = 100
UNIVERSITY_MAX = 200
DETAILS_MAX = 2000
NOTES_MAX = 2000
EMAIL_MAX = 320

# Per person, per rolling day. Counted in the database so it holds across
# every web worker, unlike the in-process rate limiter.
DAILY_LIMIT = 10
# Every message from visitors who are not signed in, together, per UTC day.
# Claimed atomically in the database (claim_anonymous_slot), so it holds across
# every worker and concurrent request: the per-address limit in the router is a
# first line, this is the ceiling however many addresses a sender uses.
ANONYMOUS_DAILY_LIMIT = 50
# Per address, per hour, for visitors who are not signed in.
ANONYMOUS_PER_IP_PER_HOUR = 5

# Deliberately loose: something@something.something with no spaces. The
# address is only shown to admins so they can reply; it is never mailed to
# automatically, so a typo costs nothing but a reply.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SuggestionLimitError(Exception):
    pass


@dataclass
class SuggestionInput:
    kind: str
    title: str = ""
    region: str = ""
    university: str = ""
    details: str = ""
    contact_email: str = ""
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return not self.errors


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def parse_suggestion(
    *,
    kind: str,
    title: str,
    region: str,
    university: str,
    details: str,
    contact_email: str = "",
) -> SuggestionInput:
    kind = kind if kind in SUGGESTION_KINDS else KIND_TOWN
    data = SuggestionInput(
        kind=kind,
        title=_clean(title),
        region=_clean(region),
        university=_clean(university),
        # Keep the person's line breaks in free text; only trim the ends.
        details=(details or "").strip(),
        contact_email=(contact_email or "").strip().lower(),
    )
    errors = data.errors
    if data.contact_email and (
        len(data.contact_email) > EMAIL_MAX or not _EMAIL_RE.match(data.contact_email)
    ):
        errors["contact_email"] = "Enter an email address like name@example.com, or leave it blank."
    if kind == KIND_TOWN:
        if not data.title:
            errors["title"] = "Enter the town or city."
        if not data.university:
            errors["university"] = "Enter the college or university there."
    elif kind == KIND_BUG:
        data.region = ""
        data.university = ""
        if not data.title:
            errors["title"] = "Say in a few words what went wrong."
        if not data.details:
            errors["details"] = "Tell us what happened and what you expected instead."
    else:
        data.region = ""
        data.university = ""
        if not data.title:
            errors["title"] = "Give your idea a short title."
        if not data.details:
            errors["details"] = "Tell us a little about how it would help you."
    for name, limit in (
        ("title", TITLE_MAX),
        ("region", REGION_MAX),
        ("university", UNIVERSITY_MAX),
        ("details", DETAILS_MAX),
    ):
        if len(getattr(data, name)) > limit and name not in errors:
            errors[name] = f"Keep this under {limit} characters."
    return data


def submitted_today(db: Session, user_id: int) -> int:
    """This person's messages in the last day."""
    since = utcnow() - timedelta(days=1)
    return (
        db.query(func.count(Suggestion.id))
        .filter(Suggestion.user_id == user_id, Suggestion.created_at >= since)
        .scalar()
        or 0
    )


def claim_anonymous_slot(db: Session, *, limit: int | None = None) -> bool:
    """Take one of today's anonymous-feedback slots, or say there are none
    left. Runs inside the caller's transaction and does not commit: the slot is
    kept only if the caller commits, so a message that fails to save gives it
    back.

    Two statements, each atomic on its own:
    1. Make sure today's row exists (INSERT ... ON CONFLICT DO NOTHING), so
       concurrent first messages of the day never collide on the key.
    2. UPDATE ... SET count = count + 1 WHERE count < limit. The database locks
       the row for the update and checks the condition against the latest
       committed count (PostgreSQL re-reads it after waiting for the lock;
       SQLite lets one writer in at a time), so however many requests race,
       at most `limit` of them see a row updated.
    """
    limit = ANONYMOUS_DAILY_LIMIT if limit is None else limit
    if limit <= 0:
        return False
    day = utcnow().date()
    dialect = db.get_bind().dialect.name
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    if dialect not in ("postgresql", "sqlite"):
        raise RuntimeError(f"claim_anonymous_slot does not support {dialect}")
    db.execute(
        insert(AnonymousFeedbackDay)
        .values(day=day, count=0)
        .on_conflict_do_nothing(index_elements=["day"])
    )
    claimed = db.execute(
        update(AnonymousFeedbackDay)
        .where(AnonymousFeedbackDay.day == day, AnonymousFeedbackDay.count < limit)
        .values(count=AnonymousFeedbackDay.count + 1)
        .execution_options(synchronize_session=False)
    )
    return claimed.rowcount == 1


def create_suggestion(
    db: Session,
    user: User | None,
    data: SuggestionInput,
    *,
    correlation_id: str | None = None,
) -> Suggestion:
    """Save a suggestion. `user` is None only for a feature idea or bug report
    from someone who is not signed in; town suggestions need an account."""
    if user is None and data.kind == KIND_TOWN:
        raise ValueError("town suggestions need a signed-in user")
    if user is None:
        # The slot and the message commit together below.
        if not claim_anonymous_slot(db):
            db.rollback()
            raise SuggestionLimitError
    elif submitted_today(db, user.id) >= DAILY_LIMIT:
        raise SuggestionLimitError
    suggestion = Suggestion(
        user_id=user.id if user else None,
        kind=data.kind,
        title=data.title,
        region=data.region or None,
        university=data.university or None,
        details=data.details or None,
        # Only kept for visitors without an account; an account has its own.
        contact_email=(data.contact_email or None) if user is None else None,
    )
    db.add(suggestion)
    db.commit()
    db.refresh(suggestion)

    if data.kind == KIND_TOWN:
        what = f"New town suggested: {suggestion.title}"
        message = f"{suggestion.title} ({suggestion.university})"
    elif data.kind == KIND_BUG:
        what = "New bug reported"
        message = suggestion.title
    else:
        what = "New feature suggested"
        message = suggestion.title
    notify(
        db,
        notification_type="suggestion_submitted",
        severity="info",
        title=what[:255],
        message=message[:1000],
        recipients=users_with_permission(db, "reports.manage"),
        dedup_fingerprint=build_dedup_fingerprint("suggestion", str(suggestion.id)),
        related_resource_type="suggestion",
        related_resource_id=suggestion.id,
        action_url=f"/admin/suggestions?kind={suggestion.kind}",
        correlation_id=correlation_id,
    )
    return suggestion


def list_for_user(
    db: Session,
    user_id: int,
    *,
    kinds: tuple[str, ...] | None = None,
    limit: int = 50,
) -> list[Suggestion]:
    query = db.query(Suggestion).filter(Suggestion.user_id == user_id)
    if kinds is not None:
        query = query.filter(Suggestion.kind.in_(kinds))
    return (
        query
        .order_by(Suggestion.created_at.desc(), Suggestion.id.desc())
        .limit(limit)
        .all()
    )


def list_suggestions(
    db: Session,
    *,
    kind: str | None = None,
    status: str | None = None,
    page: int = 1,
    per_page: int = 25,
) -> tuple[list[Suggestion], int]:
    query = db.query(Suggestion)
    if kind in SUGGESTION_KINDS:
        query = query.filter(Suggestion.kind == kind)
    if status in SUGGESTION_STATUSES:
        query = query.filter(Suggestion.status == status)
    total = query.count()
    page = max(page, 1)
    rows = (
        query.order_by(Suggestion.created_at.desc(), Suggestion.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    return rows, total


def update_suggestion(
    db: Session, suggestion: Suggestion, *, status: str, admin_notes: str
) -> None:
    if status not in SUGGESTION_STATUSES:
        raise ValueError(status)
    suggestion.status = status
    suggestion.admin_notes = admin_notes.strip()[:NOTES_MAX] or None
    suggestion.updated_at = utcnow()
    db.commit()
