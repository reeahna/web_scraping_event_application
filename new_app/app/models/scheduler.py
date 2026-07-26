"""Durable scheduler state (Phase 10).

Two small tables that make scheduling survive process death:

* ``scheduler_job_state`` — one row per scheduled website. It is both the
  per-site lock (``running`` + ``lock_holder`` + ``lock_heartbeat_at``, so at
  most one run at a time and a dead holder's lock can be reclaimed once its
  heartbeat goes stale) and the durable record of the schedule (next/last run,
  last status, a cancellation request, and a scheduler-level pause distinct
  from the site's own schedule_config.enabled).
* ``scheduler_leader`` — a single advisory row so exactly one process acts as
  the scheduler even if more than one is started by mistake. Production runs
  one dedicated scheduler process regardless; this is the safety net.

Nothing here holds executable content — only timestamps, flags, and a holder
identifier.
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import TimestampMixin

if TYPE_CHECKING:
    from app.models.website import Website


class SchedulerJobState(Base, TimestampMixin):
    __tablename__ = "scheduler_job_state"
    __table_args__ = (UniqueConstraint("website_id", name="uq_scheduler_job_state_website"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True
    )

    # Scheduler-level pause (site deactivation, city deactivation, admin pause).
    # Distinct from schedule_config.enabled, which is the source's own setting.
    paused: Mapped[bool] = mapped_column(Boolean, default=False)

    next_run_at: Mapped["datetime | None"] = mapped_column(DateTime(timezone=True), default=None)
    last_run_started_at: Mapped["datetime | None"] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_run_finished_at: Mapped["datetime | None"] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_run_status: Mapped[str | None] = mapped_column(String(32), default=None)

    # --- per-site lock ----------------------------------------------------
    running: Mapped[bool] = mapped_column(Boolean, default=False)
    lock_holder: Mapped[str | None] = mapped_column(String(128), default=None)
    lock_heartbeat_at: Mapped["datetime | None"] = mapped_column(
        DateTime(timezone=True), default=None
    )
    run_correlation_id: Mapped[str | None] = mapped_column(String(64), default=None)

    # Cooperative cancellation of the current/next run.
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)

    # Counts consecutive runs that failed for a *structural* reason (extraction
    # produced nothing / selectors stopped matching), driving the post-failure
    # re-onboarding workflow independently of transient network failures.
    consecutive_structure_failures: Mapped[int] = mapped_column(Integer, default=0)

    website: Mapped["Website"] = relationship()


class SchedulerLeader(Base, TimestampMixin):
    __tablename__ = "scheduler_leader"

    # Single logical row; the id is a fixed constant enforced in the service.
    id: Mapped[int] = mapped_column(primary_key=True)
    holder: Mapped[str] = mapped_column(String(128))
    heartbeat_at: Mapped["datetime"] = mapped_column(DateTime(timezone=True))
