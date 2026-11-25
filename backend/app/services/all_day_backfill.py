"""Mark stored events as all-day where their own extraction history says so.

Events.all_day was added after thousands of events were imported. Each import
already recorded, in the event's provenance, whether normalize found an
all-day signal from the source (ALL_DAY_MARKER in transformation_history).
This reads the latest provenance row of every event and sets all_day only
where that latest row carries the marker and the event has no stored clock
time, so it agrees with what the next import would do.

What it never does:

* mark a date-only event all-day without that marker. A missing time can mean
  "time not known" (it then stays "Time TBD");
* read a 00:00 to 23:59 span as all-day. Rows like that are reported, and the
  next import decides from the source's own wording;
* change anything but all_day: ids, URLs, times, categories, visibility,
  overrides, provenance and content_updated_at are left as they are.

The default is a dry run that changes nothing (see scripts/backfill_all_day.py).

Writing is done only through apply_all_day_to_ids: an administrator reviews the
preview, then hands over an explicit list of event ids. Each id is checked again
against the same rules (plus "not archived" and "unchanged since the review")
immediately before it is updated, and anything that no longer qualifies is
skipped with a reason instead of being marked.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import Text, and_, cast, exists, func, or_, select, update
from sqlalchemy.orm import Session

from app.extraction.normalize import ALL_DAY_MARKER
from app.models.audit_log import AuditLog
from app.models.event import Event
from app.models.event_provenance import EventProvenance
from app.models.website import Website

DAY_END_TIMES = (time(23, 59), time(23, 59, 59))


@dataclass
class AllDayBackfillReport:
    applied: bool
    # Events that would be (or were) marked all-day, by source name.
    to_mark: Counter = field(default_factory=Counter)
    marked_ids: list[int] = field(default_factory=list)
    already_all_day: int = 0
    # Date-only events without an all-day marker: left as "Time TBD".
    date_only_unmarked: Counter = field(default_factory=Counter)
    # The source's approved configuration says a date with no time means
    # "time not announced" (athletics "TBA" games): left as "Time TBD".
    time_tbd_sources: Counter = field(default_factory=Counter)
    # Marker present but a clock time is stored (an admin entry, say): left.
    marker_with_time: list[int] = field(default_factory=list)
    # Stored as 00:00 to 23:59 on one day: left for the next import to decide.
    full_day_span: Counter = field(default_factory=Counter)
    full_day_span_ids: list[int] = field(default_factory=list)

    @property
    def total_to_mark(self) -> int:
        return sum(self.to_mark.values())


def _latest_histories(db: Session) -> dict[int, list[str]]:
    """Each event's transformation history from its newest provenance row."""
    newest = (
        select(EventProvenance.event_id, func.max(EventProvenance.id).label("id"))
        .group_by(EventProvenance.event_id)
        .subquery()
    )
    rows = db.execute(
        select(EventProvenance.event_id, EventProvenance.transformation_history).join(
            newest, newest.c.id == EventProvenance.id
        )
    )
    return {event_id: list(history or []) for event_id, history in rows}


def _time_tbd_website_ids(db: Session) -> set[int]:
    """Websites whose approved configuration (what imports run on) sets
    date_only_means_time_tbd."""
    return {
        website_id
        for website_id, approved in db.execute(select(Website.id, Website.approved_pattern))
        if _means_time_tbd(approved)
    }


def _means_time_tbd(approved) -> bool:
    config = approved.get("configuration", approved) if isinstance(approved, dict) else {}
    return isinstance(config, dict) and bool(config.get("date_only_means_time_tbd"))


def backfill_all_day(db: Session, *, apply: bool = False) -> AllDayBackfillReport:
    report = AllDayBackfillReport(applied=apply)
    histories = _latest_histories(db)
    time_tbd_sites = _time_tbd_website_ids(db)
    events = db.execute(
        select(
            Event.id,
            Event.source,
            Event.start_date,
            Event.end_date,
            Event.start_time,
            Event.end_time,
            Event.all_day,
            Event.website_id,
        )
    ).all()
    for row in events:
        if row.all_day:
            report.already_all_day += 1
            continue
        marked = ALL_DAY_MARKER in histories.get(row.id, [])
        if row.start_time is None and row.end_time is None and row.start_date is not None:
            if marked and row.website_id in time_tbd_sites:
                report.time_tbd_sources[row.source] += 1
            elif marked:
                report.to_mark[row.source] += 1
                report.marked_ids.append(row.id)
            else:
                report.date_only_unmarked[row.source] += 1
            continue
        if marked:
            report.marker_with_time.append(row.id)
            continue
        if (
            row.start_time == time(0)
            and row.end_time in DAY_END_TIMES
            and row.end_date in (None, row.start_date)
        ):
            report.full_day_span[row.source] += 1
            report.full_day_span_ids.append(row.id)

    if apply and report.marked_ids:
        for start in range(0, len(report.marked_ids), 500):
            chunk = report.marked_ids[start : start + 500]
            # A plain column update: no ORM events, so content_updated_at and
            # the AI queue are untouched (the visitor-facing facts are not new).
            db.execute(
                update(Event)
                .where(Event.id.in_(chunk))
                .values(all_day=True)
                .execution_options(synchronize_session=False)
            )
        db.commit()
    return report


# --- applying a reviewed list of event ids -------------------------------------

SKIP_NOT_FOUND = "not found"
SKIP_DUPLICATE_ID = "listed more than once"
SKIP_ARCHIVED = "archived"
SKIP_ALREADY_ALL_DAY = "already all-day"
SKIP_HAS_TIME = "has a start or end time"
SKIP_NO_DATE = "no start date"
SKIP_NOT_FLAGGED = "latest extraction does not flag it all-day"
SKIP_TIME_TBD_SOURCE = 'source is set to "time not announced"'
SKIP_CHANGED = "details changed after the review"
SKIP_UNVERIFIED = "can't confirm it is unchanged since the review"
SKIP_CHANGED_DURING_APPLY = "changed while applying"


@dataclass
class AllDayIdApplyReport:
    applied: bool
    reviewed_at: datetime
    requested: int = 0
    # Ids that were (or, on a check, would be) marked all-day.
    changed_ids: list[int] = field(default_factory=list)
    # (id, reason) for every id left alone.
    skipped: list[tuple[int, str]] = field(default_factory=list)

    @property
    def skipped_by_reason(self) -> Counter:
        return Counter(reason for _, reason in self.skipped)


def parse_event_ids(text: str) -> list[int]:
    """Event ids from pasted text: digits separated by commas, spaces or new
    lines; a leading # is allowed (as the preview prints them), and a line
    starting with "# " is a comment. Anything else is refused rather than
    guessed at."""
    ids = []
    for line in text.splitlines():
        if line.strip() == "#" or line.lstrip().startswith("# "):
            continue
        for token in line.replace(",", " ").split():
            token = token.lstrip("#")
            if not token.isdigit():
                raise ValueError(f"Not an event id: {token!r}")
            ids.append(int(token))
    return ids


def _utc(value: datetime | None) -> datetime | None:
    """Aware UTC (SQLite hands datetimes back naive; they are stored as UTC)."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True)
class _Verified:
    """What an event was checked against, so the update can insist on it."""

    provenance_id: int
    website_id: int | None
    # The website's approved configuration exactly as stored (JSON text).
    approved_text: str | None


def _latest_provenance(db: Session, event_id: int) -> tuple[int, list[str]] | None:
    row = db.execute(
        select(EventProvenance.id, EventProvenance.transformation_history)
        .where(EventProvenance.event_id == event_id)
        .order_by(EventProvenance.id.desc())
        .limit(1)
    ).first()
    return None if row is None else (row.id, list(row.transformation_history or []))


def _approved_text(db: Session, website_id: int | None) -> str | None:
    if website_id is None:
        return None
    return db.execute(
        select(cast(Website.approved_pattern, Text)).where(Website.id == website_id)
    ).scalar_one_or_none()


def _unchanged_since(event: Event, reviewed_at: datetime) -> str | None:
    """None when the event provably has not changed since the review.

    content_updated_at is when visitor-facing details last changed. Events
    listed before it was tracked have none; for those only updated_at, which
    every write to the row moves, can prove nothing happened since. Without
    either proof the event is skipped, never assumed unchanged."""
    changed = _utc(event.content_updated_at)
    if changed is not None:
        return SKIP_CHANGED if changed > reviewed_at else None
    written = _utc(event.updated_at)
    if written is None or written > reviewed_at:
        return SKIP_UNVERIFIED
    return None


def _skip_reason(
    db: Session, event: Event | None, reviewed_at: datetime
) -> tuple[str | None, _Verified | None]:
    """Why this event must not be marked now (or None), and, when it still
    qualifies exactly as the preview counted it, what it was checked against."""
    if event is None:
        return SKIP_NOT_FOUND, None
    if event.archived_at is not None:
        return SKIP_ARCHIVED, None
    if event.all_day:
        return SKIP_ALREADY_ALL_DAY, None
    if event.start_time is not None or event.end_time is not None:
        return SKIP_HAS_TIME, None
    if event.start_date is None:
        return SKIP_NO_DATE, None
    latest = _latest_provenance(db, event.id)
    if latest is None or ALL_DAY_MARKER not in latest[1]:
        return SKIP_NOT_FLAGGED, None
    approved_text = _approved_text(db, event.website_id)
    if approved_text is not None and _means_time_tbd(json.loads(approved_text)):
        return SKIP_TIME_TBD_SOURCE, None
    reason = _unchanged_since(event, reviewed_at)
    if reason is not None:
        return reason, None
    return None, _Verified(latest[0], event.website_id, approved_text)


def _still_eligible(event_id: int, verified: _Verified, reviewed_at: datetime):
    """Every eligibility rule as SQL, for the update's WHERE: the event row as
    checked, its newest provenance row still the one that carried the marker,
    and its source's approved configuration byte for byte the one that was
    read. If anything moved after the check, the update matches no row."""
    newest_provenance = (
        select(func.max(EventProvenance.id))
        .where(EventProvenance.event_id == Event.id)
        .scalar_subquery()
    )
    conditions = [
        Event.id == event_id,
        Event.archived_at.is_(None),
        Event.all_day.is_(False),
        Event.start_time.is_(None),
        Event.end_time.is_(None),
        Event.start_date.is_not(None),
        or_(
            and_(
                Event.content_updated_at.is_not(None),
                Event.content_updated_at <= reviewed_at,
            ),
            and_(Event.content_updated_at.is_(None), Event.updated_at <= reviewed_at),
        ),
        newest_provenance == verified.provenance_id,
        exists().where(
            EventProvenance.id == verified.provenance_id,
            EventProvenance.event_id == event_id,
            cast(EventProvenance.transformation_history, Text).contains(
                json.dumps(ALL_DAY_MARKER), autoescape=True
            ),
        ),
    ]
    if verified.website_id is None:
        conditions.append(Event.website_id.is_(None))
    else:
        approved = cast(Website.approved_pattern, Text)
        conditions += [
            Event.website_id == verified.website_id,
            exists().where(
                Website.id == verified.website_id,
                approved.is_(None) if verified.approved_text is None
                else approved == verified.approved_text,
            ),
        ]
    return conditions


def apply_all_day_to_ids(
    db: Session,
    event_ids: list[int],
    *,
    reviewed_at: datetime,
    apply: bool = False,
    commit: bool = True,
) -> AllDayIdApplyReport:
    """Marks all-day only the listed events that still qualify. With
    apply=False it reports what would happen and writes nothing.

    Each event is re-read (locked where the database supports it) and checked
    immediately before its own update, and the update's WHERE repeats every
    rule (see _still_eligible), so a change made in between leaves the event
    alone and reports it. Only events.all_day is written: dates, times,
    visibility, categories, overrides, duplicate decisions, provenance and
    content_updated_at are untouched.

    commit=False leaves an apply uncommitted so the caller can commit it
    together with its audit entry."""
    reviewed_at = _utc(reviewed_at)
    report = AllDayIdApplyReport(applied=apply, reviewed_at=reviewed_at)
    report.requested = len(event_ids)
    seen: set[int] = set()
    for event_id in event_ids:
        if event_id in seen:
            report.skipped.append((event_id, SKIP_DUPLICATE_ID))
            continue
        seen.add(event_id)
        event = db.execute(
            select(Event).where(Event.id == event_id).with_for_update()
        ).scalar_one_or_none()
        reason, verified = _skip_reason(db, event, reviewed_at)
        if reason is not None:
            report.skipped.append((event_id, reason))
            continue
        if apply:
            # A plain column update: no ORM events, so content_updated_at and
            # the AI queue stay as they are.
            result = db.execute(
                update(Event)
                .where(*_still_eligible(event_id, verified, reviewed_at))
                .values(all_day=True)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                report.skipped.append((event_id, SKIP_CHANGED_DURING_APPLY))
                continue
        report.changed_ids.append(event_id)
    if not apply:
        db.rollback()
    elif commit:
        db.commit()
    return report


# --- binding Apply to an earlier Check -----------------------------------------

CHECK_ACTION = "events_all_day_checked_by_id"
APPLY_ACTION = "events_all_day_applied_by_id"
# The Apply audit entry points at the Check it used (entity_id = its audit id).
CHECK_ENTITY = "all_day_check"
CHECK_VALID_FOR = timedelta(hours=1)


class CheckRejected(ValueError):
    """Apply refused: no usable earlier Check. Nothing was changed."""


@dataclass(frozen=True)
class ReviewedCheck:
    check_id: int
    reviewed_at: datetime
    # Exactly the ids that Check found would change; Apply touches no others.
    event_ids: list[int]


def check_audit_payload(report: AllDayIdApplyReport, listed_ids: list[int]) -> dict:
    """What a Check records server-side, and all that a later Apply may use."""
    return {
        "reviewed_at": report.reviewed_at.isoformat(),
        "listed_ids": listed_ids,
        "would_change_ids": report.changed_ids,
        "skipped": [{"event_id": i, "reason": reason} for i, reason in report.skipped],
    }


def load_check_for_apply(
    db: Session, check_id: int, *, user_id: int, now: datetime | None = None
) -> ReviewedCheck:
    """The earlier Check an Apply must match: recorded by this server (an audit
    entry, which nobody can edit), by the same person, within the last hour,
    not used yet, and with something to change. Locks the entry so two
    Applies of one Check cannot both run."""
    now = _utc(now or datetime.now(UTC))
    entry = db.execute(
        select(AuditLog).where(AuditLog.id == check_id).with_for_update()
    ).scalar_one_or_none()
    if entry is None or entry.action != CHECK_ACTION or entry.after_state is None:
        raise CheckRejected("No matching check was found. Run Check first.")
    if entry.user_id != user_id:
        raise CheckRejected("That check was run by someone else. Run Check yourself first.")
    checked_at = _utc(entry.created_at)
    if checked_at > now or now - checked_at > CHECK_VALID_FOR:
        raise CheckRejected("That check is more than an hour old. Run Check again.")
    used = db.execute(
        select(AuditLog.id).where(
            AuditLog.action == APPLY_ACTION,
            AuditLog.entity_type == CHECK_ENTITY,
            AuditLog.entity_id == check_id,
        )
    ).first()
    if used is not None:
        raise CheckRejected("That check has already been applied. Run Check again.")
    payload = json.loads(entry.after_state)
    ids = [int(i) for i in payload.get("would_change_ids") or []]
    if not ids:
        raise CheckRejected("That check found nothing to change.")
    return ReviewedCheck(check_id, _utc(datetime.fromisoformat(payload["reviewed_at"])), ids)
