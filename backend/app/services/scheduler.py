"""Durable scheduler service (Phase 10).

The decision logic — is a site eligible, is a lock stale, when does the next
run fall — is pure and unit-testable here; the APScheduler wiring and the
process entry point live in ``app.scheduler`` and call into this module. Only
this module opens a Session for scheduler state, mirroring the extraction
engine's "one place touches the DB" discipline.

Locking is per-site and durable: a run sets ``running`` + a ``lock_holder`` +
a heartbeat on the site's ``SchedulerJobState`` row. A second fire for the same
site sees ``running`` and skips (no overlap). If the holding process dies, its
heartbeat stops advancing; once it is older than the stale threshold any
scheduler may reclaim the lock, so a crash never leaves a site stuck forever.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.onboarding import ACTIVE
from app.models.scheduler import SchedulerJobState, SchedulerLeader
from app.models.website import Website
from app.schemas.extraction import SiteConfiguration
from app.schemas.schedule import ScheduleConfig, parse_schedule_config

# A lock whose heartbeat is older than this is treated as abandoned by a dead
# process and may be reclaimed. Comfortably longer than the heartbeat interval.
STALE_LOCK_SECONDS = 600
# The leader row is considered vacant if its heartbeat is older than this.
STALE_LEADER_SECONDS = 90
LEADER_ROW_ID = 1
# Consecutive structural failures that trigger the re-onboarding workflow.
STRUCTURE_FAILURE_THRESHOLD = 3
# The single source of truth for how many website imports run at once — shared
# by the scheduler dispatch loop and the manual bulk-import runner so both
# obey the same concurrency ceiling.
MAX_CONCURRENT_RUNS = 4


@dataclass(frozen=True)
class ScheduleEligibility:
    eligible: bool
    reasons: tuple[str, ...] = ()
    schedule: ScheduleConfig | None = None


@dataclass(frozen=True)
class ManualImportEligibility:
    """Whether a website may be imported *now* by an administrator (manual
    one-off / bulk). Deliberately does NOT require an enabled automatic
    schedule — an admin may keep auto-imports off yet still import on demand."""

    eligible: bool
    reasons: tuple[str, ...] = ()
    execution_strategy: str = "http"


def _source_eligibility_reasons(website: Website) -> list[str]:
    """The reasons a source is not importable at all — independent of any
    automatic schedule. Shared by scheduled eligibility and manual-import
    eligibility so the two never drift apart."""
    reasons: list[str] = []
    if website.onboarding_status != ACTIVE:
        reasons.append(f"onboarding_status is '{website.onboarding_status}', not active")
    if not website.is_active:
        reasons.append("website is not active")
    if website.archived_at is not None:
        reasons.append("website is archived")
    if website.city is None:
        reasons.append("website has no city")
    elif not website.city.is_active:
        reasons.append("city is not active")
    if not website.approved_pattern:
        reasons.append("no approved configuration")
    else:
        try:
            SiteConfiguration.model_validate(_approved_config_dict(website))
        except Exception as exc:  # noqa: BLE001 - any validation failure means not importable
            reasons.append(f"approved configuration is invalid: {type(exc).__name__}")
    return reasons


def _approved_execution_strategy(website: Website) -> str:
    config = _approved_config_dict(website)
    if isinstance(config, dict):
        return "browser" if config.get("execution_strategy") == "browser" else "http"
    return "http"


def evaluate_eligibility(website: Website) -> ScheduleEligibility:
    """Pure: a website is schedulable only if it is an approved, active source
    of an active city with a valid approved configuration and a valid, enabled
    schedule. Returns every failing reason, not just the first."""
    reasons = _source_eligibility_reasons(website)

    schedule: ScheduleConfig | None = None
    try:
        schedule = parse_schedule_config(website.schedule_config)
    except Exception as exc:  # noqa: BLE001
        reasons.append(f"schedule is invalid: {type(exc).__name__}")
    if schedule is None:
        reasons.append("no schedule configured")
    elif not schedule.enabled:
        reasons.append("schedule is disabled")

    return ScheduleEligibility(eligible=not reasons, reasons=tuple(reasons), schedule=schedule)


def evaluate_manual_import_eligibility(website: Website) -> ManualImportEligibility:
    """Whether an administrator may import this website now. Uses only the
    source-level checks — an approved, active, non-archived website of an active
    city — NOT whether automatic scheduling is enabled."""
    reasons = _source_eligibility_reasons(website)
    return ManualImportEligibility(
        eligible=not reasons,
        reasons=tuple(reasons),
        execution_strategy=_approved_execution_strategy(website),
    )


def _approved_config_dict(website: Website) -> dict:
    approved = website.approved_pattern or {}
    # The approved snapshot stores the frozen SiteConfiguration under
    # "configuration"; older snapshots may store it at the top level.
    if isinstance(approved, dict) and "configuration" in approved:
        return approved["configuration"]
    return approved


def compute_next_run_at(schedule: ScheduleConfig, now: datetime) -> datetime:
    return now + timedelta(minutes=schedule.interval_minutes)


def lock_is_stale(state: SchedulerJobState, now: datetime, threshold_seconds: int) -> bool:
    """A running lock whose heartbeat is older than the threshold (or missing)
    is abandoned and reclaimable. A non-running row is never 'stale'."""
    if not state.running:
        return False
    if state.lock_heartbeat_at is None:
        return True
    return (now - _aware(state.lock_heartbeat_at)).total_seconds() > threshold_seconds


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


# --- DB operations -----------------------------------------------------------


def get_or_create_state(db: Session, website_id: int) -> SchedulerJobState:
    state = db.scalar(
        select(SchedulerJobState).where(SchedulerJobState.website_id == website_id)
    )
    if state is None:
        state = SchedulerJobState(website_id=website_id)
        db.add(state)
        db.commit()
        db.refresh(state)
    return state


def try_acquire(
    db: Session, website_id: int, holder: str, *, now: datetime | None = None
) -> SchedulerJobState | None:
    """Atomically claim the per-site lock. Returns the state row on success, or
    None if the site is already running (with a fresh lock) or paused. A stale
    lock is reclaimed. Serialized by the row itself: the commit either wins or
    the caller sees the other holder."""
    now = now or datetime.now(UTC)
    state = get_or_create_state(db, website_id)
    if state.paused:
        return None
    if state.running and not lock_is_stale(state, now, STALE_LOCK_SECONDS):
        return None
    state.running = True
    state.lock_holder = holder
    state.lock_heartbeat_at = now
    state.last_run_started_at = now
    state.cancel_requested = False
    db.commit()
    db.refresh(state)
    return state


def heartbeat(db: Session, state: SchedulerJobState, *, now: datetime | None = None) -> None:
    state.lock_heartbeat_at = now or datetime.now(UTC)
    db.commit()


def release_lock(
    db: Session,
    state: SchedulerJobState,
    *,
    status: str,
    schedule: ScheduleConfig | None,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    state.running = False
    state.lock_holder = None
    state.lock_heartbeat_at = None
    state.run_correlation_id = None
    state.last_run_finished_at = now
    state.last_run_status = status
    if schedule is not None:
        state.next_run_at = compute_next_run_at(schedule, now)
    db.commit()


def request_cancel(db: Session, website_id: int) -> bool:
    state = db.scalar(
        select(SchedulerJobState).where(SchedulerJobState.website_id == website_id)
    )
    if state is None:
        return False
    state.cancel_requested = True
    db.commit()
    return True


def set_paused(db: Session, website_id: int, paused: bool) -> None:
    state = get_or_create_state(db, website_id)
    state.paused = paused
    db.commit()


def pause_city_sites(db: Session, city_id: int, paused: bool) -> int:
    """Pause (or resume) every scheduler job for a city's websites. Used by
    city deactivation/reactivation; events and history are untouched."""
    website_ids = db.scalars(select(Website.id).where(Website.city_id == city_id)).all()
    count = 0
    for website_id in website_ids:
        set_paused(db, website_id, paused)
        count += 1
    return count


def reclaim_stale_locks(db: Session, *, now: datetime | None = None) -> list[int]:
    """Release any lock whose holder appears dead (stale heartbeat). Returns the
    website ids reclaimed. Startup and periodic maintenance both call this so a
    process crash cannot leave a site permanently 'running'."""
    now = now or datetime.now(UTC)
    reclaimed: list[int] = []
    for state in db.scalars(select(SchedulerJobState).where(SchedulerJobState.running.is_(True))):
        if lock_is_stale(state, now, STALE_LOCK_SECONDS):
            state.running = False
            state.lock_holder = None
            state.lock_heartbeat_at = None
            state.last_run_status = "recovered_stale"
            reclaimed.append(state.website_id)
    if reclaimed:
        db.commit()
    return reclaimed


@dataclass
class Reconciliation:
    eligible_website_ids: list[int] = field(default_factory=list)
    paused_website_ids: list[int] = field(default_factory=list)
    reclaimed_website_ids: list[int] = field(default_factory=list)


def reconcile(db: Session, *, now: datetime | None = None) -> Reconciliation:
    """Bring durable state in line with reality: reclaim stale locks, ensure a
    state row exists per eligible site, and pause sites that are no longer
    eligible. Idempotent — safe to run on every startup and periodically."""
    now = now or datetime.now(UTC)
    result = Reconciliation()
    result.reclaimed_website_ids = reclaim_stale_locks(db, now=now)

    for website in db.scalars(select(Website)):
        eligibility = evaluate_eligibility(website)
        state = get_or_create_state(db, website.id)
        if eligibility.eligible:
            if state.paused:
                state.paused = False
            if state.next_run_at is None and eligibility.schedule is not None:
                state.next_run_at = compute_next_run_at(eligibility.schedule, now)
            result.eligible_website_ids.append(website.id)
        else:
            if not state.paused:
                state.paused = True
            result.paused_website_ids.append(website.id)
    db.commit()
    return result


# --- leader election ---------------------------------------------------------


def try_become_leader(db: Session, holder: str, *, now: datetime | None = None) -> bool:
    """Claim the single leader row if vacant or stale. A dedicated deployment
    runs one scheduler process, but this guards against two being started."""
    now = now or datetime.now(UTC)
    leader = db.get(SchedulerLeader, LEADER_ROW_ID)
    if leader is None:
        db.add(SchedulerLeader(id=LEADER_ROW_ID, holder=holder, heartbeat_at=now))
        db.commit()
        return True
    stale = (now - _aware(leader.heartbeat_at)).total_seconds() > STALE_LEADER_SECONDS
    if leader.holder == holder or stale:
        leader.holder = holder
        leader.heartbeat_at = now
        db.commit()
        return True
    return False


def leader_heartbeat(db: Session, holder: str, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    leader = db.get(SchedulerLeader, LEADER_ROW_ID)
    if leader is None or leader.holder != holder:
        return False
    leader.heartbeat_at = now
    db.commit()
    return True


@dataclass(frozen=True)
class SchedulerHealth:
    leader_holder: str | None
    leader_heartbeat_at: datetime | None
    leader_is_fresh: bool
    scheduled_count: int
    running_count: int
    paused_count: int


def scheduler_health(db: Session, *, now: datetime | None = None) -> SchedulerHealth:
    now = now or datetime.now(UTC)
    leader = db.get(SchedulerLeader, LEADER_ROW_ID)
    states = list(db.scalars(select(SchedulerJobState)))
    fresh = (
        leader is not None
        and (now - _aware(leader.heartbeat_at)).total_seconds() <= STALE_LEADER_SECONDS
    )
    return SchedulerHealth(
        leader_holder=leader.holder if leader else None,
        leader_heartbeat_at=leader.heartbeat_at if leader else None,
        leader_is_fresh=fresh,
        scheduled_count=sum(1 for s in states if not s.paused),
        running_count=sum(1 for s in states if s.running),
        paused_count=sum(1 for s in states if s.paused),
    )
