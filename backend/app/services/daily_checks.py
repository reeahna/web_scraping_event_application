"""The daily-check baseline: every active, approved source is checked at least
once every 24 hours.

``plan_daily_checks`` is a read-only audit that sorts every source into what
would happen to it. ``apply_daily_checks`` acts on that plan, and only for the
sources an administrator picked:

* A source that should be checked but has **no schedule** (or an invalid one)
  is the one case the baseline fills in by itself, the same daily default
  ``ensure_default_schedule`` gives on activation.
* A source whose automatic imports were **switched off**, or that runs **less
  often than daily**, carries a stored administrator choice. It is listed and
  changed only when an administrator ticks it, so nothing intentional is
  overwritten without being reported first.
* A source whose **latest check failed** is never switched on or sped up here,
  so a failing site is not asked more often. Its existing schedule, retries
  and backoff stay exactly as they are.
* Blocked, archived, unapproved and otherwise ineligible sources are never
  scheduled.

Only ``schedule_config`` and the scheduler's job row change. Approved
configurations, the scraping engine and event data are never touched. Newly
switched-on sources have their first check spread over the next day rather
than all firing together, so the one-at-a-time import queue and the sites
themselves see an even load.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models.website import Website
from app.schemas.schedule import ScheduleConfig, parse_schedule_config
from app.services.audit import record_audit
from app.services.scheduler import evaluate_eligibility, get_or_create_state
from app.services.source_freshness import (
    DAILY_TARGET_MINUTES,
    TARGET_MET,
    TARGET_MISSING,
    TARGET_NOT_APPLICABLE,
    TARGET_OFF,
    TARGET_SLOW,
    SourceFreshness,
    source_freshness,
)

AUDIT_ACTION = "schedule_daily_baseline"


@dataclass(frozen=True)
class PlannedSource:
    website: Website
    freshness: SourceFreshness
    stored_interval_minutes: int | None

    @property
    def failing(self) -> bool:
        return self.freshness.failing


@dataclass
class DailyCheckPlan:
    """Every source, sorted by what the daily baseline means for it."""

    meets: list[PlannedSource] = field(default_factory=list)
    missing: list[PlannedSource] = field(default_factory=list)   # filled in automatically
    off: list[PlannedSource] = field(default_factory=list)       # admin choice: listed only
    slow: list[PlannedSource] = field(default_factory=list)      # admin choice: listed only
    held: list[PlannedSource] = field(default_factory=list)      # failing: left alone
    not_scheduled: int = 0                                        # blocked, archived, ...

    @property
    def should_be_checked(self) -> int:
        return len(self.meets) + len(self.missing) + len(self.off) + len(self.slow) + len(
            self.held
        )

    @property
    def overdue(self) -> list[PlannedSource]:
        everyone = self.meets + self.missing + self.off + self.slow + self.held
        return [item for item in everyone if item.freshness.overdue]

    @property
    def changeable_ids(self) -> set[int]:
        return {item.website.id for item in self.missing + self.off + self.slow}


def _stored_interval(website: Website) -> int | None:
    try:
        schedule = parse_schedule_config(website.schedule_config)
    except Exception:  # noqa: BLE001
        return None
    return schedule.interval_minutes if schedule else None


def plan_daily_checks(db: Session, *, now: datetime | None = None) -> DailyCheckPlan:
    """Read-only: where every source stands against the daily baseline."""
    sites = list(
        db.scalars(
            select(Website).options(joinedload(Website.city)).order_by(Website.name)
        ).unique()
    )
    fresh = source_freshness(db, sites, now=now)
    plan = DailyCheckPlan()
    for site in sites:
        item = PlannedSource(site, fresh[site.id], _stored_interval(site))
        target = item.freshness.target
        if target == TARGET_NOT_APPLICABLE:
            plan.not_scheduled += 1
        elif target == TARGET_MET:
            plan.meets.append(item)
        elif item.failing:
            plan.held.append(item)
        elif target == TARGET_MISSING:
            plan.missing.append(item)
        elif target == TARGET_OFF:
            plan.off.append(item)
        elif target == TARGET_SLOW:
            plan.slow.append(item)
    return plan


@dataclass
class DailyCheckResult:
    changed: list[Website] = field(default_factory=list)
    skipped: list[Website] = field(default_factory=list)


def apply_daily_checks(
    db: Session,
    *,
    website_ids: Collection[int],
    actor_id: int | None,
    correlation_id: str | None = None,
    ip_address: str | None = None,
    now: datetime | None = None,
) -> DailyCheckResult:
    """Put the picked sources on the daily schedule, keeping each one's retry
    and backoff settings. A picked id that is no longer changeable (it became
    failing, was archived, or already meets the target) is skipped, so a
    stale form can never schedule a source it should not. Each change is
    audited with the schedule before and after."""
    now = now or datetime.now(UTC)
    plan = plan_daily_checks(db, now=now)
    allowed = plan.changeable_ids
    picked = [item for item in plan.missing + plan.off + plan.slow if item.website.id in
              set(website_ids)]
    result = DailyCheckResult()
    result.skipped = [
        site for site in db.scalars(select(Website).where(Website.id.in_(list(website_ids))))
        if site.id not in allowed
    ]
    # Spread first checks evenly over one interval so the newly scheduled
    # sources queue one after another instead of all coming due together.
    spacing = timedelta(minutes=DAILY_TARGET_MINUTES) / max(len(picked), 1)
    for index, item in enumerate(picked):
        site = item.website
        before = site.schedule_config
        try:
            current = parse_schedule_config(before)
        except Exception:  # noqa: BLE001 - an invalid stored schedule is replaced
            current = None
        base = current.model_dump() if current else {}
        base.update(enabled=True, interval_minutes=DAILY_TARGET_MINUTES)
        site.schedule_config = ScheduleConfig.model_validate(base).model_dump()
        db.flush()
        state = get_or_create_state(db, site.id)
        if evaluate_eligibility(site).eligible:
            state.paused = False
            state.next_run_at = now + spacing * index + timedelta(minutes=5)
        record_audit(
            db,
            actor_id=actor_id,
            action=AUDIT_ACTION,
            entity_type="website",
            entity_id=site.id,
            before={"schedule_config": before},
            after={"schedule_config": site.schedule_config},
            correlation_id=correlation_id,
            ip_address=ip_address,
            detail=f"Daily check baseline ({item.freshness.target_label.lower()} before)",
        )
        result.changed.append(site)
    db.commit()
    return result
