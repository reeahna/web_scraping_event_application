"""Record and read the latest outcome of each scheduler background job.

The scheduler calls `record_job_success` / `record_job_failure` after a tick
does real work or fails. Recording is best effort: a database hiccup while
writing the record is logged and swallowed, because a status write must never
take down the job it describes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.background_job import BackgroundJobStatus

logger = get_logger("services.job_status")

_ERROR_LIMIT = 1000
_RESULT_LIMIT = 255


@dataclass(frozen=True)
class JobInfo:
    key: str
    label: str
    cadence: str
    # Hours without a successful run after which the job counts as overdue.
    # None for jobs that only report when there is work (they may legitimately
    # go a long time without anything to do).
    stale_after_hours: float | None = None


# The scheduler's recurring background jobs, in the order the health page lists
# them. Per-website scrapes are shown separately (scheduler_job_state).
BACKGROUND_JOBS: tuple[JobInfo, ...] = (
    JobInfo("categorization", "AI categorization", "Every minute, when events are waiting"),
    JobInfo("onboarding", "Onboarding queue", "Every minute, when jobs are queued"),
    JobInfo("geocoding", "Map locations", "Every minute, when events need a location"),
    JobInfo("alerts", "Alerts and reminders", "Every minute"),
    JobInfo("bulk_import", "Bulk imports", "Every minute, when an import is queued"),
    JobInfo("college_towns", "Daily college town import", "Daily and after each deploy", 30),
    JobInfo("home_town_sweep", "Out-of-town sweep", "Daily and after each deploy", 30),
    JobInfo("attendability_sweep", "Non-event sweep", "Daily and after each deploy", 30),
    JobInfo("category_photos", "Placeholder photo refill", "Hourly"),
)

JOB_LABELS = {job.key: job.label for job in BACKGROUND_JOBS}


def _now() -> datetime:
    return datetime.now(UTC)


def _row(db: Session, job: str) -> BackgroundJobStatus:
    row = db.scalar(select(BackgroundJobStatus).where(BackgroundJobStatus.job == job))
    if row is None:
        row = BackgroundJobStatus(job=job, consecutive_failures=0)
        db.add(row)
    return row


def record_job_success(
    session_factory: Callable[[], Session],
    job: str,
    result: str | None = None,
    *,
    now: datetime | None = None,
) -> None:
    now = now or _now()
    db = session_factory()
    try:
        row = _row(db, job)
        row.last_run_at = now
        row.last_success_at = now
        row.last_status = "ok"
        if result is not None:
            row.last_result = result[:_RESULT_LIMIT]
        row.consecutive_failures = 0
        row.paused_until = None
        db.commit()
    except Exception as exc:  # noqa: BLE001 - status writes are best effort
        db.rollback()
        logger.warning("could not record success for job %s: %s", job, exc)
    finally:
        db.close()


def record_job_failure(
    session_factory: Callable[[], Session],
    job: str,
    error: str,
    *,
    paused_until: datetime | None = None,
    now: datetime | None = None,
) -> None:
    now = now or _now()
    db = session_factory()
    try:
        row = _row(db, job)
        row.last_run_at = now
        row.last_failure_at = now
        row.last_status = "failed"
        row.last_error = (error or "unknown error")[:_ERROR_LIMIT]
        row.consecutive_failures = (row.consecutive_failures or 0) + 1
        row.paused_until = paused_until
        db.commit()
    except Exception as exc:  # noqa: BLE001 - status writes are best effort
        db.rollback()
        logger.warning("could not record failure for job %s: %s", job, exc)
    finally:
        db.close()


def job_statuses(db: Session) -> dict[str, BackgroundJobStatus]:
    return {row.job: row for row in db.scalars(select(BackgroundJobStatus))}
