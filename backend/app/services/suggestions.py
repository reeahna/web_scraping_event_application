"""Visitor suggestions: new towns (with their university) and new features.

Signed-in visitors submit them from /suggest; admins holding reports.view see
them in /admin/suggestions and those holding reports.manage change their status.
"""

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models.suggestion import Suggestion
from app.models.user import User
from app.services.notifications import build_dedup_fingerprint, notify
from app.services.rbac import users_with_permission

KIND_TOWN = "town"
KIND_FEATURE = "feature"
SUGGESTION_KINDS: tuple[str, ...] = (KIND_TOWN, KIND_FEATURE)

SUGGESTION_STATUSES: dict[str, str] = {
    "new": "New",
    "planned": "Planned",
    "done": "Added",
    "declined": "Not planned",
}

TITLE_MAX = 200
REGION_MAX = 100
UNIVERSITY_MAX = 200
DETAILS_MAX = 2000
NOTES_MAX = 2000

# Per person, per rolling day. Counted in the database so it holds across
# every web worker, unlike the in-process rate limiter.
DAILY_LIMIT = 10


class SuggestionLimitError(Exception):
    pass


@dataclass
class SuggestionInput:
    kind: str
    title: str = ""
    region: str = ""
    university: str = ""
    details: str = ""
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return not self.errors


def _clean(value: str | None) -> str:
    return " ".join((value or "").split())


def parse_suggestion(
    *, kind: str, title: str, region: str, university: str, details: str
) -> SuggestionInput:
    kind = kind if kind in SUGGESTION_KINDS else KIND_TOWN
    data = SuggestionInput(
        kind=kind,
        title=_clean(title),
        region=_clean(region),
        university=_clean(university),
        # Keep the person's line breaks in free text; only trim the ends.
        details=(details or "").strip(),
    )
    errors = data.errors
    if kind == KIND_TOWN:
        if not data.title:
            errors["title"] = "Enter the town or city."
        if not data.university:
            errors["university"] = "Enter the college or university there."
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
    since = utcnow() - timedelta(days=1)
    return (
        db.query(func.count(Suggestion.id))
        .filter(Suggestion.user_id == user_id, Suggestion.created_at >= since)
        .scalar()
        or 0
    )


def create_suggestion(
    db: Session, user: User, data: SuggestionInput, *, correlation_id: str | None = None
) -> Suggestion:
    if submitted_today(db, user.id) >= DAILY_LIMIT:
        raise SuggestionLimitError
    suggestion = Suggestion(
        user_id=user.id,
        kind=data.kind,
        title=data.title,
        region=data.region or None,
        university=data.university or None,
        details=data.details or None,
    )
    db.add(suggestion)
    db.commit()
    db.refresh(suggestion)

    if data.kind == KIND_TOWN:
        what = f"New town suggested: {suggestion.title}"
        message = f"{suggestion.title} ({suggestion.university})"
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


def list_for_user(db: Session, user_id: int, *, limit: int = 50) -> list[Suggestion]:
    return (
        db.query(Suggestion)
        .filter(Suggestion.user_id == user_id)
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
