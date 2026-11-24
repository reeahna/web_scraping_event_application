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

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, time

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.extraction.normalize import ALL_DAY_MARKER
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
    ids = set()
    for website_id, approved in db.execute(select(Website.id, Website.approved_pattern)):
        config = approved.get("configuration", approved) if isinstance(approved, dict) else {}
        if isinstance(config, dict) and config.get("date_only_means_time_tbd"):
            ids.add(website_id)
    return ids


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


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _latest_history(db: Session, event_id: int) -> list[str]:
    history = db.execute(
        select(EventProvenance.transformation_history)
        .where(EventProvenance.event_id == event_id)
        .order_by(EventProvenance.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return list(history or [])


def _skip_reason(
    db: Session, event: Event | None, reviewed_at: datetime, time_tbd_sites: set[int]
) -> str | None:
    """Why this event must not be marked now, or None when it still qualifies
    exactly as the preview counted it."""
    if event is None:
        return SKIP_NOT_FOUND
    if event.archived_at is not None:
        return SKIP_ARCHIVED
    if event.all_day:
        return SKIP_ALREADY_ALL_DAY
    if event.start_time is not None or event.end_time is not None:
        return SKIP_HAS_TIME
    if event.start_date is None:
        return SKIP_NO_DATE
    if ALL_DAY_MARKER not in _latest_history(db, event.id):
        return SKIP_NOT_FLAGGED
    if event.website_id in time_tbd_sites:
        return SKIP_TIME_TBD_SOURCE
    changed = _aware(event.content_updated_at)
    if changed is not None and changed > reviewed_at:
        return SKIP_CHANGED
    return None


def apply_all_day_to_ids(
    db: Session, event_ids: list[int], *, reviewed_at: datetime, apply: bool = False
) -> AllDayIdApplyReport:
    """Marks all-day only the listed events that still qualify. With
    apply=False it reports what would happen and writes nothing.

    Each event is re-read (locked where the database supports it) and checked
    immediately before its own update, and the update itself repeats the
    conditions, so an import that gives the event a time in between leaves it
    alone. Only events.all_day is written: dates, times, visibility,
    categories, overrides, duplicate decisions, provenance and
    content_updated_at are untouched."""
    reviewed_at = _aware(reviewed_at)
    report = AllDayIdApplyReport(applied=apply, reviewed_at=reviewed_at)
    report.requested = len(event_ids)
    time_tbd_sites = _time_tbd_website_ids(db)
    seen: set[int] = set()
    for event_id in event_ids:
        if event_id in seen:
            report.skipped.append((event_id, SKIP_DUPLICATE_ID))
            continue
        seen.add(event_id)
        event = db.execute(
            select(Event).where(Event.id == event_id).with_for_update()
        ).scalar_one_or_none()
        reason = _skip_reason(db, event, reviewed_at, time_tbd_sites)
        if reason is not None:
            report.skipped.append((event_id, reason))
            continue
        if apply:
            # A plain column update guarded by the same conditions: no ORM
            # events, so content_updated_at and the AI queue stay as they are.
            result = db.execute(
                update(Event)
                .where(
                    Event.id == event_id,
                    Event.all_day.is_(False),
                    Event.start_time.is_(None),
                    Event.end_time.is_(None),
                    Event.archived_at.is_(None),
                )
                .values(all_day=True)
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                report.skipped.append((event_id, SKIP_CHANGED_DURING_APPLY))
                continue
        report.changed_ids.append(event_id)
    if apply:
        db.commit()
    else:
        db.rollback()
    return report
