from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.onboarding_jobs import BATCH_OPEN
from app.database import Base
from app.models.base import utcnow

if TYPE_CHECKING:
    from app.models.city import City
    from app.models.onboarding_job import OnboardingJob
    from app.models.user import User


class OnboardingBatch(Base):
    """One bulk submission of source URLs.

    Only the counts that describe the *submission* are stored
    (submitted/valid/invalid, plus how many jobs have finished). Per-outcome
    counts — how many are ready, needs_review, failed, ... — are derived on
    read with a GROUP BY over this batch's jobs
    (app.repositories.onboarding.status_counts). Storing six more counters
    would mean six more values that can drift out of step with the rows they
    summarize.
    """

    __tablename__ = "onboarding_batches"

    id: Mapped[int] = mapped_column(primary_key=True)
    submitted_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    default_city_id: Mapped[int | None] = mapped_column(
        ForeignKey("cities.id", ondelete="SET NULL"), default=None
    )
    default_timezone: Mapped[str | None] = mapped_column(String(64), default=None)
    # Off by default: an existing website's approved configuration is never
    # re-detected unless the administrator explicitly asked for it.
    redetect_existing: Mapped[bool] = mapped_column(Boolean, default=False)
    source_kind: Mapped[str] = mapped_column(String(16), default="paste")

    submitted_count: Mapped[int] = mapped_column(Integer, default=0)
    valid_count: Mapped[int] = mapped_column(Integer, default=0)
    invalid_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, default=0)

    status: Mapped[str] = mapped_column(String(16), default=BATCH_OPEN, index=True)
    # Free-text, per-row problems found at submission time (bad URL, unknown
    # city slug, ...). Kept on the batch rather than as jobs, because a row
    # that never produced a valid URL has nothing to process.
    # JSON list of {"row": int, "value": str, "reason": str}
    rejected_rows: Mapped[list | None] = mapped_column(JSON, default=None)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    submitted_by: Mapped["User | None"] = relationship()
    default_city: Mapped["City | None"] = relationship()
    jobs: Mapped[list["OnboardingJob"]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="OnboardingJob.row_number"
    )
