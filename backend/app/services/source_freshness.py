"""How fresh each source's listing is, for administrators.

Bulletin's baseline is that every active, approved source is checked at least
once every 24 hours. This module reads the durable records that already exist
(``ExtractionRun`` history, ``SchedulerJobState`` and the source's own
``schedule_config``) and answers, per source:

* when it last completed a successful check,
* when the scheduler will next check it,
* how its latest failed check ended, and whether that failure is current,
* whether it meets the daily target, and
* whether it is overdue: more than ``OVERDUE_GRACE`` past its expected interval.

It never writes, never schedules and never fetches anything. The town-level
"Listings checked" time stays in ``app.services.event_freshness``; this module
only says which source is holding it back.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.base import as_aware_utc
from app.models.extraction_run import ExtractionRun
from app.models.scheduler import SchedulerJobState
from app.models.website import Website
from app.schemas.schedule import parse_schedule_config
from app.services.event_freshness import PERSISTENT_RUN_TYPES, SUCCESSFUL_STATUSES
from app.services.scheduler import _source_eligibility_reasons

# Every active, approved source should be checked at least this often.
DAILY_TARGET_MINUTES = 1440
# A source is flagged overdue once its last successful check is older than
# its expected interval plus this much. Enough slack for a queue of
# one-at-a-time imports and a retry or two, small enough to notice a day lost.
OVERDUE_GRACE = timedelta(hours=12)

# Daily-target outcomes, worst first so a list sorted by them leads with the
# sources that need a person.
TARGET_OFF = "off"                  # should be checked, but automatic imports are off
TARGET_MISSING = "missing"          # should be checked, but has no (valid) schedule
TARGET_SLOW = "slow"                # scheduled, but less often than daily
TARGET_MET = "met"                  # scheduled at least daily
TARGET_NOT_APPLICABLE = "n/a"       # blocked, archived, unapproved, ...: never scheduled

TARGET_LABELS = {
    TARGET_OFF: "Automatic imports off",
    TARGET_MISSING: "No schedule",
    TARGET_SLOW: "Less often than daily",
    TARGET_MET: "Daily or more often",
    TARGET_NOT_APPLICABLE: "Not scheduled",
}


@dataclass(frozen=True)
class SourceFreshness:
    website_id: int
    # Whether this source is one Bulletin should be checking at all: active,
    # approved, not archived, in an active town. Blocked, archived and
    # unapproved sources are never scheduled and never flagged.
    should_be_checked: bool
    target: str
    # The cadence the source is held to: its own schedule when that is on,
    # otherwise the daily baseline.
    expected_interval_minutes: int
    last_success_at: datetime | None
    next_check_at: datetime | None
    running: bool
    latest_failure_at: datetime | None
    latest_failure_status: str | None
    latest_failure_summary: str | None
    # The latest check failed (a failure newer than the last success).
    failing: bool
    consecutive_failures: int
    overdue: bool
    # How long since the source was last known fresh, when overdue.
    overdue_by: timedelta | None

    @property
    def target_label(self) -> str:
        return TARGET_LABELS[self.target]

    @property
    def meets_target(self) -> bool:
        return self.target == TARGET_MET

    @property
    def needs_attention(self) -> bool:
        """Anything an administrator should look at: overdue, failing, or a
        source that should be checked daily but is not scheduled to be."""
        if not self.should_be_checked:
            return False
        return self.overdue or self.failing or not self.meets_target


def _target(website: Website, should_be_checked: bool) -> tuple[str, int]:
    if not should_be_checked:
        return TARGET_NOT_APPLICABLE, DAILY_TARGET_MINUTES
    try:
        schedule = parse_schedule_config(website.schedule_config)
    except Exception:  # noqa: BLE001 - an invalid stored schedule is "no schedule"
        schedule = None
    if schedule is None:
        return TARGET_MISSING, DAILY_TARGET_MINUTES
    if not schedule.enabled:
        return TARGET_OFF, DAILY_TARGET_MINUTES
    if schedule.interval_minutes > DAILY_TARGET_MINUTES:
        return TARGET_SLOW, schedule.interval_minutes
    return TARGET_MET, schedule.interval_minutes


def source_freshness(
    db: Session, websites: Iterable[Website], *, now: datetime | None = None
) -> dict[int, SourceFreshness]:
    """Freshness for each of ``websites``, keyed by website id. Three grouped
    queries, however many websites are passed."""
    sites = list(websites)
    if not sites:
        return {}
    now = now or datetime.now(UTC)
    ids = [site.id for site in sites]
    finished = func.coalesce(ExtractionRun.completed_at, ExtractionRun.started_at)
    persistent = (
        ExtractionRun.website_id.in_(ids),
        ExtractionRun.run_type.in_(PERSISTENT_RUN_TYPES),
    )
    last_success = {
        website_id: as_aware_utc(at)
        for website_id, at in db.query(ExtractionRun.website_id, func.max(finished))
        .filter(*persistent, ExtractionRun.status.in_(SUCCESSFUL_STATUSES))
        .group_by(ExtractionRun.website_id)
    }
    latest_failure_ids = (
        db.query(func.max(ExtractionRun.id))
        .filter(*persistent, ExtractionRun.status.notin_(SUCCESSFUL_STATUSES))
        .group_by(ExtractionRun.website_id)
    )
    latest_failure = {
        run.website_id: run
        for run in db.scalars(select(ExtractionRun).where(ExtractionRun.id.in_(latest_failure_ids)))
    }
    states = {
        state.website_id: state
        for state in db.scalars(
            select(SchedulerJobState).where(SchedulerJobState.website_id.in_(ids))
        )
    }

    result: dict[int, SourceFreshness] = {}
    for site in sites:
        should_be_checked = not _source_eligibility_reasons(site)
        target, interval = _target(site, should_be_checked)
        success_at = last_success.get(site.id)
        failure = latest_failure.get(site.id)
        failure_at = (
            as_aware_utc(failure.completed_at or failure.started_at) if failure else None
        )
        failing = failure_at is not None and (success_at is None or failure_at > success_at)

        state = states.get(site.id)
        scheduled = target in (TARGET_MET, TARGET_SLOW)
        next_at = None
        running = bool(state and state.running)
        if scheduled and state is not None and not state.paused and state.next_run_at:
            next_at = as_aware_utc(state.next_run_at)

        # A source that has never completed a check is measured from its
        # approval, so a newly approved source is not overdue on day one.
        fresh_since = success_at or (as_aware_utc(site.approved_at) if site.approved_at else None)
        overdue_by = None
        if should_be_checked and fresh_since is not None:
            age = now - fresh_since
            if age > timedelta(minutes=interval) + OVERDUE_GRACE:
                overdue_by = age

        result[site.id] = SourceFreshness(
            website_id=site.id,
            should_be_checked=should_be_checked,
            target=target,
            expected_interval_minutes=interval,
            last_success_at=success_at,
            next_check_at=next_at,
            running=running,
            latest_failure_at=failure_at,
            latest_failure_status=failure.status if failure else None,
            latest_failure_summary=failure.error_summary if failure else None,
            failing=failing,
            consecutive_failures=site.consecutive_failure_count or 0,
            overdue=overdue_by is not None,
            overdue_by=overdue_by,
        )
    return result


def attention_website_ids(db: Session, *, now: datetime | None = None) -> list[int]:
    """Every source that should be checked and is overdue, failing, or not on
    a daily schedule."""
    sites = list(db.scalars(select(Website).where(Website.archived_at.is_(None))))
    return [
        website_id
        for website_id, fresh in source_freshness(db, sites, now=now).items()
        if fresh.needs_attention
    ]


def format_age(delta: timedelta | None) -> str:
    """'14 hours', '50 hours', '4 days': how long, in whole hours under three
    days and whole days after that."""
    if delta is None:
        return ""
    hours = int(delta.total_seconds() // 3600)
    if hours >= 72:
        return f"{hours // 24} days"
    return f"{hours} hour{'s' if hours != 1 else ''}"
