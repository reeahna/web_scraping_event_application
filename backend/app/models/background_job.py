"""Last-run record for the scheduler's background jobs.

The scheduler process runs a handful of recurring jobs besides per-website
scrapes (AI categorization, alerts, geocoding, photo refills, sweeps, the
college-town import). Their failures used to exist only in that process's
logs and memory, so the web app could not show that, say, AI categorization
had been failing for hours. One row per job keeps its latest outcome where the
admin System health page can read it.

Only timestamps, short result text and a trimmed error message; never secrets.
"""

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.models.base import TimestampMixin


class BackgroundJobStatus(Base, TimestampMixin):
    __tablename__ = "background_job_status"
    __table_args__ = (UniqueConstraint("job", name="uq_background_job_status_job"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    job: Mapped[str] = mapped_column(String(64))

    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_failure_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # "ok" or "failed": the outcome of the latest run.
    last_status: Mapped[str | None] = mapped_column(String(16), default=None)
    # Short human summary of the latest successful run ("labeled 12 events").
    last_result: Mapped[str | None] = mapped_column(String(255), default=None)
    last_error: Mapped[str | None] = mapped_column(String(1000), default=None)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    # Set when the job backs off after a failure or quota limit.
    paused_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
