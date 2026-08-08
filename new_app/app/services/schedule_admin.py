"""Admin-facing presentation and control of a website's automatic-import
schedule, built entirely on the existing durable scheduler (app.services.
scheduler + SchedulerJobState). Nothing here starts a scheduler or runs an
extraction — it reads/writes the same schedule_config + scheduler state the
dedicated ``python -m app.scheduler`` process acts on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.website import Website
from app.schemas.schedule import (
    MAX_INTERVAL_MINUTES,
    MIN_INTERVAL_MINUTES,
    ScheduleConfig,
    parse_schedule_config,
)
from app.services.scheduler import (
    STALE_LEADER_SECONDS,
    ScheduleEligibility,
    compute_next_run_at,
    evaluate_eligibility,
    get_or_create_state,
    scheduler_health,
)

# The schedule an approved+active source is given when it has none, so
# "approved + active" reliably means "imports automatically" (default daily).
DEFAULT_INTERVAL_MINUTES = 1440
DEFAULT_SCHEDULE_CONFIG: dict = {"enabled": True, "interval_minutes": DEFAULT_INTERVAL_MINUTES}

# Interval presets offered in the edit form (minutes). Custom values are still
# accepted within [MIN_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES].
INTERVAL_PRESETS: tuple[int, ...] = (15, 30, 60, 180, 360, 720, 1440, 2880, 10080)


def format_interval(minutes: int | None) -> str:
    """One readable rendering of a cadence, used everywhere: 15 -> 'Every 15
    minutes', 60 -> 'Every hour', 1440 -> 'Every 24 hours', 10080 -> 'Every 7
    days'."""
    if not minutes or minutes <= 0:
        return "—"
    if minutes % 1440 == 0:
        days = minutes // 1440
        return "Every 24 hours" if days == 1 else f"Every {days} days"
    if minutes % 60 == 0:
        hours = minutes // 60
        return "Every hour" if hours == 1 else f"Every {hours} hours"
    return f"Every {minutes} minutes"


def _admin_zone() -> ZoneInfo:
    try:
        return ZoneInfo(get_settings().app_timezone)
    except Exception:  # noqa: BLE001 - a bad env value must not break the page
        return ZoneInfo("UTC")


def format_admin_datetime(value: datetime | None) -> str:
    """A timestamp rendered in the configured application timezone (never a
    naive UTC string). None -> '—'."""
    if value is None:
        return "—"
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    local = aware.astimezone(_admin_zone())
    hour = local.hour % 12 or 12
    ampm = "AM" if local.hour < 12 else "PM"
    return f"{local.strftime('%b')} {local.day}, {local.year} at {hour}:{local.minute:02d} {ampm}"


# --- default schedule (approval + activation) --------------------------------


def ensure_default_schedule(website: Website) -> bool:
    """Give an approved, active website a default automatic schedule when it has
    NONE at all. Returns True if a schedule was written.

    Preserves an administrator's explicit choices: a stored schedule (enabled OR
    disabled) is never overwritten, so a deliberately disabled schedule is not
    silently re-enabled on reactivation."""
    if website.schedule_config is not None:
        return False  # already configured (possibly explicitly disabled) — leave it
    if not website.approved_pattern or not website.is_active:
        return False
    website.schedule_config = dict(DEFAULT_SCHEDULE_CONFIG)
    return True


# --- per-website schedule view -----------------------------------------------


@dataclass
class WebsiteScheduleView:
    status_label: str          # Enabled / Disabled / Not configured / Not eligible
    status_detail: str
    frequency: str             # "Every 24 hours" or "—"
    interval_minutes: int | None
    next_run_display: str
    last_run_display: str
    scheduler_state: str       # Scheduled / Running / Paused / Overdue / Disabled / ...
    execution: str             # Browser / HTTP
    enabled: bool
    configured: bool
    eligible: bool
    ineligible_reasons: list[str]


def _schedule_status(eligibility: ScheduleEligibility, website: Website) -> tuple[str, str]:
    raw = website.schedule_config
    if raw is None:
        return "Not configured", "This Website does not have an automatic import schedule."
    schedule = None
    try:
        schedule = parse_schedule_config(raw)
    except Exception:  # noqa: BLE001
        return "Invalid", "The stored automatic import schedule is not valid."
    if schedule is not None and not schedule.enabled:
        return "Disabled", "Automatic imports are disabled for this Website."
    # A valid enabled schedule, but the source itself may not be importable.
    source_reasons = [r for r in eligibility.reasons if "schedule" not in r]
    if source_reasons:
        return (
            "Not eligible",
            "Automatic imports require an active Website with an approved configuration.",
        )
    return "Enabled", "Automatic imports are enabled for this Website."


def derive_scheduler_state(state, eligibility: ScheduleEligibility, *, now: datetime) -> str:
    """The live scheduler state for this website from its durable job row."""
    if not eligibility.eligible:
        # Distinguish an explicitly disabled schedule from a truly ineligible source.
        if any("schedule is disabled" in r for r in eligibility.reasons):
            return "Disabled"
        if any("no schedule configured" in r for r in eligibility.reasons):
            return "Waiting for initial schedule"
        return "Not eligible"
    if state is None:
        return "Waiting for initial schedule"
    if state.running:
        return "Running"
    if state.paused:
        return "Paused"
    if state.next_run_at is None:
        return "Waiting for initial schedule"
    next_at = state.next_run_at
    if next_at.tzinfo is None:
        next_at = next_at.replace(tzinfo=UTC)
    # More than one dispatch interval past due suggests the scheduler is behind
    # or not running.
    if (now - next_at).total_seconds() > 120:
        return "Overdue"
    return "Scheduled"


def build_website_schedule_view(db: Session, website: Website) -> WebsiteScheduleView:
    now = datetime.now(UTC)
    eligibility = evaluate_eligibility(website)
    state = get_or_create_state(db, website.id)
    status_label, status_detail = _schedule_status(eligibility, website)

    schedule = None
    try:
        schedule = parse_schedule_config(website.schedule_config)
    except Exception:  # noqa: BLE001
        schedule = None
    interval = schedule.interval_minutes if schedule else None
    execution = "Browser" if _execution_strategy(website) == "browser" else "HTTP"

    return WebsiteScheduleView(
        status_label=status_label,
        status_detail=status_detail,
        frequency=format_interval(interval) if (schedule and schedule.enabled) else "—",
        interval_minutes=interval,
        next_run_display=format_admin_datetime(state.next_run_at),
        last_run_display=format_admin_datetime(state.last_run_finished_at),
        scheduler_state=derive_scheduler_state(state, eligibility, now=now),
        execution=execution,
        enabled=bool(schedule and schedule.enabled),
        configured=website.schedule_config is not None,
        eligible=eligibility.eligible,
        ineligible_reasons=[r for r in eligibility.reasons if "schedule" not in r],
    )


def _execution_strategy(website: Website) -> str:
    approved = website.approved_pattern or {}
    config = approved.get("configuration", approved) if isinstance(approved, dict) else {}
    return "browser" if isinstance(config, dict) and config.get("execution_strategy") == "browser" \
        else "http"


# --- schedule editing + scoped reconcile -------------------------------------


def build_schedule_config(*, enabled: bool, interval_minutes: int) -> dict:
    """A validated schedule_config dict from form fields. Raises ValueError
    (via pydantic) when out of the [15 min, 30 day] bounds."""
    interval_minutes = max(MIN_INTERVAL_MINUTES, min(MAX_INTERVAL_MINUTES, int(interval_minutes)))
    return ScheduleConfig(enabled=enabled, interval_minutes=interval_minutes).model_dump()


def apply_schedule(db: Session, website: Website, schedule_config: dict) -> None:
    """Persist the schedule and immediately reconcile THIS website's durable job
    row (no waiting for the 5-minute global reconcile):

    - a valid enabled schedule for an eligible source: unpause and set
      next_run_at = now + interval (so a shortened cadence takes effect on the
      next dispatch rather than at the old, later time);
    - a disabled schedule: pause the job so it stops being dispatched.
    """
    website.schedule_config = schedule_config
    db.commit()
    db.refresh(website)

    state = get_or_create_state(db, website.id)
    eligibility = evaluate_eligibility(website)
    now = datetime.now(UTC)
    if eligibility.eligible and eligibility.schedule is not None:
        state.paused = False
        state.next_run_at = compute_next_run_at(eligibility.schedule, now)
    else:
        # Disabled or otherwise ineligible: stop dispatching. The next_run_at is
        # left as-is but paused==True keeps it out of the due query.
        state.paused = True
    db.commit()


# --- scheduler process health ------------------------------------------------


@dataclass
class SchedulerProcessStatus:
    status: str            # Running / Stale / Not detected
    detail: str
    holder: str | None
    heartbeat_display: str
    scheduled_count: int
    running_count: int
    paused_count: int


def describe_scheduler_process(db: Session) -> SchedulerProcessStatus:
    health = scheduler_health(db)
    if health.leader_holder is None:
        status, detail = "Not detected", (
            "No scheduler process has ever registered. Automatic imports require "
            "the dedicated scheduler process: python -m app.scheduler"
        )
    elif health.leader_is_fresh:
        status, detail = "Running", (
            f"A scheduler process is active (heartbeat within {STALE_LEADER_SECONDS}s)."
        )
    else:
        status, detail = "Stale", (
            "The last scheduler heartbeat is stale — the scheduler process may have "
            "stopped. Restart it with: python -m app.scheduler"
        )
    return SchedulerProcessStatus(
        status=status,
        detail=detail,
        holder=health.leader_holder,
        heartbeat_display=format_admin_datetime(health.leader_heartbeat_at),
        scheduled_count=health.scheduled_count,
        running_count=health.running_count,
        paused_count=health.paused_count,
    )
