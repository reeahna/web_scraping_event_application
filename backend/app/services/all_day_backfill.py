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
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import time

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
