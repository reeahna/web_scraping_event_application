"""Everything the admin System health page shows, gathered in one place.

The page answers "is Bulletin working right now?" without opening the
scheduler, websites, events and reports pages one by one. Each section is
computed here from data the web process can read (the database and settings);
nothing calls out to a third party.

`collect_system_health` also turns what it finds into a short list of issues
for the summary at the top, so a reader who sees "All systems normal" can stop
there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.production_checks import production_blockers
from app.models.base import as_aware_utc
from app.models.city import City
from app.models.event import Event
from app.models.extraction_run import ExtractionRun
from app.models.scheduler import SchedulerJobState
from app.models.website import Website
from app.services.changelog import APP_VERSION
from app.services.job_status import BACKGROUND_JOBS, job_statuses
from app.services.schedule_admin import describe_scheduler_process

PROBLEM = "problem"
WARNING = "warning"

# A scheduled scrape this late is "overdue": the dispatcher checks every
# minute, so an hour behind means it is not being picked up.
OVERDUE_GRACE = timedelta(hours=1)
# Events waiting this long for an AI label mean categorization is not keeping
# up (it labels a batch a minute when healthy).
AI_BACKLOG_GRACE = timedelta(hours=3)
FAILING_SOURCES_SHOWN = 10


@dataclass
class Issue:
    severity: str  # PROBLEM | WARNING
    text: str
    url: str | None = None


@dataclass
class JobRow:
    key: str
    label: str
    cadence: str
    state: str  # ok | failed | paused | overdue | never | off
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_result: str | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    paused_until: datetime | None = None


@dataclass
class FailingSource:
    website_id: int
    name: str
    city: str | None
    failures: int
    last_success_at: datetime | None
    last_failure_at: datetime | None
    error: str | None


@dataclass
class TownRow:
    city_id: int
    name: str
    slug: str
    upcoming_events: int
    active_sources: int


@dataclass
class SystemHealth:
    now: datetime
    issues: list[Issue]
    scheduler: object
    overdue_scrapes: int
    runs_24h: dict[str, int]
    runs_24h_total: int
    sources_ok: int
    sources_failing: int
    failing_sources: list[FailingSource]
    ai: dict
    jobs: list[JobRow]
    towns: list[TownRow]
    towns_without_events: int
    version: str
    blockers: list = field(default_factory=list)


def _aware(value: datetime | None) -> datetime | None:
    return as_aware_utc(value) if value is not None else None


def time_ago(value: datetime | None, now: datetime | None = None) -> str:
    """'just now', '5 minutes ago', '3 hours ago', '2 days ago', or 'never'."""
    if value is None:
        return "never"
    now = now or datetime.now(UTC)
    seconds = (now - as_aware_utc(value)).total_seconds()
    if seconds < 0:
        seconds = -seconds
        suffix, prefix = "", "in "
    else:
        suffix, prefix = " ago", ""
    if seconds < 60:
        return "just now"
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            n = int(seconds // size)
            return f"{prefix}{n} {unit}{'' if n == 1 else 's'}{suffix}"
    return "just now"  # pragma: no cover


# --- sections ------------------------------------------------------------------


def _overdue_scrapes(db: Session, now: datetime) -> int:
    return (
        db.scalar(
            select(func.count(SchedulerJobState.id)).where(
                SchedulerJobState.paused.is_(False),
                SchedulerJobState.running.is_(False),
                SchedulerJobState.next_run_at.is_not(None),
                SchedulerJobState.next_run_at < now - OVERDUE_GRACE,
            )
        )
        or 0
    )


def _runs_last_24h(db: Session, now: datetime) -> dict[str, int]:
    rows = db.execute(
        select(ExtractionRun.status, func.count(ExtractionRun.id))
        .where(ExtractionRun.started_at >= now - timedelta(hours=24))
        .group_by(ExtractionRun.status)
    ).all()
    return {status: count for status, count in rows}


def _live_sources(db: Session):
    return db.query(Website).filter(Website.is_active.is_(True), Website.archived_at.is_(None))


def _failing_sources(db: Session) -> tuple[int, int, list[FailingSource]]:
    live = _live_sources(db)
    total = live.count()
    failing_q = live.filter(Website.consecutive_failure_count > 0)
    failing_count = failing_q.count()
    worst = (
        failing_q.order_by(
            Website.consecutive_failure_count.desc(), Website.last_failure_at.desc()
        )
        .limit(FAILING_SOURCES_SHOWN)
        .all()
    )
    rows: list[FailingSource] = []
    for website in worst:
        error = db.scalar(
            select(ExtractionRun.error_summary)
            .where(
                ExtractionRun.website_id == website.id,
                ExtractionRun.error_summary.is_not(None),
            )
            .order_by(ExtractionRun.started_at.desc())
            .limit(1)
        )
        rows.append(
            FailingSource(
                website_id=website.id,
                name=website.source_display_name or website.name,
                city=website.city.name if website.city else None,
                failures=website.consecutive_failure_count,
                last_success_at=website.last_success_at,
                last_failure_at=website.last_failure_at,
                error=error,
            )
        )
    return total - failing_count, failing_count, rows


def _upcoming_filter(today: date):
    return and_(
        Event.is_active.is_(True),
        Event.archived_at.is_(None),
        or_(
            and_(Event.end_date.is_not(None), Event.end_date >= today),
            and_(Event.end_date.is_(None), Event.start_date >= today),
        ),
    )


def _towns(db: Session, today: date) -> list[TownRow]:
    events = dict(
        db.execute(
            select(Event.city_id, func.count(Event.id))
            .where(_upcoming_filter(today))
            .group_by(Event.city_id)
        ).all()
    )
    sources = dict(
        db.execute(
            select(Website.city_id, func.count(Website.id))
            .where(Website.is_active.is_(True), Website.archived_at.is_(None))
            .group_by(Website.city_id)
        ).all()
    )
    cities = db.scalars(select(City).where(City.is_active.is_(True))).all()
    rows = [
        TownRow(
            city_id=c.id,
            name=c.name,
            slug=c.slug,
            upcoming_events=events.get(c.id, 0),
            active_sources=sources.get(c.id, 0),
        )
        for c in cities
    ]
    # Empty towns first (they need a look), then the thinnest.
    rows.sort(key=lambda r: (r.upcoming_events, r.name.lower()))
    return rows


def _ai_section(db: Session, jobs: dict) -> dict:
    from app.services.ai_categorization import (
        pending_events,
        provider_name,
        scheduled_categorization_enabled,
    )

    settings = get_settings()
    enabled = scheduled_categorization_enabled()
    provider = provider_name()
    model = None
    if provider == "groq":
        model = settings.groq_model
    elif provider == "gemini":
        model = getattr(settings, "gemini_model", None)

    pending = pending_events(db)
    pending_count = pending.count()
    oldest_waiting = (
        pending.with_entities(func.min(Event.created_at)).order_by(None).scalar()
        if pending_count
        else None
    )
    labeled = (
        db.query(func.count(Event.id)).filter(Event.category_source == "ai").scalar() or 0
    )
    row = jobs.get("categorization")
    return {
        "enabled": enabled,
        "provider": provider,
        "model": model,
        "pending": pending_count,
        "oldest_waiting": _aware(oldest_waiting),
        "labeled_total": labeled,
        "status": row,
    }


def _job_rows(jobs: dict, now: datetime, ai_enabled: bool) -> list[JobRow]:
    settings = get_settings()
    enabled = {
        "categorization": ai_enabled,
        "geocoding": settings.geocoding_enabled,
        "category_photos": bool(settings.unsplash_access_key),
        "home_town_sweep": settings.home_town_filter_enabled,
        "attendability_sweep": settings.attendability_filter_enabled,
        "college_towns": settings.college_town_import_enabled,
    }
    rows = []
    for info in BACKGROUND_JOBS:
        row = jobs.get(info.key)
        state = "never"
        if not enabled.get(info.key, True):
            state = "off"
        elif row is not None:
            paused_until = _aware(row.paused_until)
            if row.last_status == "failed":
                state = "paused" if paused_until and paused_until > now else "failed"
            else:
                state = "ok"
            last_success = _aware(row.last_success_at)
            if (
                state == "ok"
                and info.stale_after_hours
                and last_success
                and now - last_success > timedelta(hours=info.stale_after_hours)
            ):
                state = "overdue"
        rows.append(
            JobRow(
                key=info.key,
                label=info.label,
                cadence=info.cadence,
                state=state,
                last_run_at=_aware(row.last_run_at) if row else None,
                last_success_at=_aware(row.last_success_at) if row else None,
                last_result=row.last_result if row else None,
                last_error=row.last_error if row else None,
                consecutive_failures=row.consecutive_failures if row else 0,
                paused_until=_aware(row.paused_until) if row else None,
            )
        )
    return rows


# --- summary -------------------------------------------------------------------


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _issues(health: SystemHealth, waiting_rows: list[dict] | None) -> list[Issue]:
    issues: list[Issue] = []
    now = health.now

    status = health.scheduler.status
    if status == "Not detected":
        issues.append(
            Issue(PROBLEM, "The scheduler has never run, so nothing is scraped automatically.",
                  "/admin/scheduler")
        )
    elif status == "Stale":
        issues.append(
            Issue(PROBLEM, "The scheduler has stopped checking in. Scrapes and AI labels "
                  "are not running.", "/admin/scheduler")
        )

    ai = health.ai
    if ai["enabled"]:
        row = ai["status"]
        if row is not None and row.last_status == "failed":
            issues.append(
                Issue(PROBLEM, f"AI categorization is failing: {row.last_error}", "#ai")
            )
        oldest = ai["oldest_waiting"]
        if oldest is not None and now - oldest > AI_BACKLOG_GRACE:
            issues.append(
                Issue(
                    WARNING,
                    f"{_plural(ai['pending'], 'event has', 'events have')} been waiting "
                    f"for an AI label since {time_ago(oldest, now)}.",
                    "#ai",
                )
            )

    for job in health.jobs:
        if job.key == "categorization":
            continue  # covered above with more detail
        if job.state in ("failed", "paused"):
            issues.append(Issue(PROBLEM, f"{job.label} failed: {job.last_error}", "#jobs"))
        elif job.state == "overdue":
            issues.append(
                Issue(WARNING, f"{job.label} has not run since "
                      f"{time_ago(job.last_success_at, now)}.", "#jobs")
            )

    if health.overdue_scrapes:
        late = _plural(health.overdue_scrapes, "scheduled scrape is", "scheduled scrapes are")
        issues.append(Issue(WARNING, f"{late} more than an hour late.", "/admin/scheduler"))
    if health.sources_failing:
        failing = _plural(health.sources_failing, "live source is", "live sources are")
        issues.append(Issue(WARNING, f"{failing} failing.", "#scraping"))
    if health.towns_without_events:
        issues.append(
            Issue(
                WARNING,
                f"{health.towns_without_events} of {len(health.towns)} towns have no "
                "upcoming events.",
                "#towns",
            )
        )
    for row in waiting_rows or []:
        if row["url"].endswith("detecting"):
            continue  # in progress, nothing to do
        issues.append(Issue(WARNING, f"{row['count']} {row['label']}.", row["url"]))
    # Production settings are summarized in one line: they rarely change, and
    # listing each one here would keep the summary permanently red and teach the
    # reader to skip it. The Deploy section lists them in full.
    blockers = sum(1 for b in health.blockers if b.severity == "blocker")
    if blockers:
        settings = _plural(blockers, "production setting needs", "production settings need")
        issues.append(Issue(WARNING, f"{settings} attention.", "#deploy"))

    issues.sort(key=lambda i: 0 if i.severity == PROBLEM else 1)
    return issues


def collect_system_health(
    db: Session,
    *,
    waiting_rows: list[dict] | None = None,
    now: datetime | None = None,
) -> SystemHealth:
    now = now or datetime.now(UTC)
    jobs = job_statuses(db)
    ai = _ai_section(db, jobs)
    runs = _runs_last_24h(db, now)
    sources_ok, sources_failing, failing = _failing_sources(db)
    towns = _towns(db, now.date())

    health = SystemHealth(
        now=now,
        issues=[],
        scheduler=describe_scheduler_process(db),
        overdue_scrapes=_overdue_scrapes(db, now),
        runs_24h=runs,
        runs_24h_total=sum(runs.values()),
        sources_ok=sources_ok,
        sources_failing=sources_failing,
        failing_sources=failing,
        ai=ai,
        jobs=_job_rows(jobs, now, ai["enabled"]),
        towns=towns,
        towns_without_events=sum(1 for t in towns if t.upcoming_events == 0),
        version=APP_VERSION,
        blockers=production_blockers(get_settings()),
    )
    health.issues = _issues(health, waiting_rows)
    return health
