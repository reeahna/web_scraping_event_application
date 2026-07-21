from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.onboarding_jobs import QUEUED
from app.database import Base
from app.models.base import utcnow

if TYPE_CHECKING:
    from app.models.city import City
    from app.models.extraction_run import ExtractionRun
    from app.models.onboarding_batch import OnboardingBatch
    from app.models.user import User
    from app.models.website import Website


class OnboardingJob(Base):
    """One submitted source URL and everything that happened to it.

    Exists independently of `Website` on purpose: a row that is a duplicate,
    or that failed before a website could be created, still needs somewhere
    to record what was submitted and why nothing came of it.

    Status model (see app.core.onboarding_jobs): `status` is the single
    authoritative value — it advances through the processing steps and stops
    on a terminal value, and that terminal value *is* the outcome, so there
    is no separate `outcome` column to disagree with it. `current_step` is a
    diagnostic breadcrumb only: the last step attempted, kept after a failure
    so the UI can say where the job broke. Nothing branches on it.
    """

    __tablename__ = "onboarding_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("onboarding_batches.id", ondelete="CASCADE"), index=True, default=None
    )
    row_number: Mapped[int] = mapped_column(Integer, default=0)

    submitted_url: Mapped[str] = mapped_column(String(2000))
    # Canonicalized via app.services.fingerprints.normalize_url — the value
    # every duplicate check compares against, so it is indexed.
    normalized_url: Mapped[str] = mapped_column(String(2000), index=True)
    final_url: Mapped[str | None] = mapped_column(String(2000), default=None)

    city_id: Mapped[int | None] = mapped_column(
        ForeignKey("cities.id", ondelete="SET NULL"), default=None
    )
    timezone_override: Mapped[str | None] = mapped_column(String(64), default=None)
    submitted_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    # Optional per-row metadata supplied in a CSV; when absent, both are
    # inferred from the page (see app.extraction.inference.site_metadata).
    submitted_name: Mapped[str | None] = mapped_column(String(255), default=None)
    submitted_source_display_name: Mapped[str | None] = mapped_column(String(255), default=None)

    status: Mapped[str] = mapped_column(String(32), default=QUEUED, index=True)
    current_step: Mapped[str | None] = mapped_column(String(32), default=None)

    website_id: Mapped[int | None] = mapped_column(
        ForeignKey("websites.id", ondelete="SET NULL"), index=True, default=None
    )
    # Set when this URL resolved to a website that already existed; the job
    # links to it rather than creating a second row for the same source.
    duplicate_of_website_id: Mapped[int | None] = mapped_column(
        ForeignKey("websites.id", ondelete="SET NULL"), default=None
    )

    detected_pattern: Mapped[str | None] = mapped_column(String(64), default=None)
    detection_confidence: Mapped[float | None] = mapped_column(default=None)
    detection_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="SET NULL"), default=None
    )
    preview_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="SET NULL"), default=None
    )
    configuration_version: Mapped[int | None] = mapped_column(Integer, default=None)

    events_found: Mapped[int] = mapped_column(Integer, default=0)
    events_valid: Mapped[int] = mapped_column(Integer, default=0)
    events_rejected: Mapped[int] = mapped_column(Integer, default=0)
    # PreviewQualityResult.as_dict() — plain numbers, no page content.
    quality: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # {"field": value, ...} plus an "inferred_fields" list, so the UI can mark
    # which site metadata was guessed rather than supplied.
    inferred_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    failure_reason: Mapped[str | None] = mapped_column(String(500), default=None)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    batch: Mapped["OnboardingBatch | None"] = relationship(back_populates="jobs")
    city: Mapped["City | None"] = relationship()
    submitted_by: Mapped["User | None"] = relationship()
    website: Mapped["Website | None"] = relationship(foreign_keys=[website_id])
    duplicate_of_website: Mapped["Website | None"] = relationship(
        foreign_keys=[duplicate_of_website_id]
    )
    detection_run: Mapped["ExtractionRun | None"] = relationship(foreign_keys=[detection_run_id])
    preview_run: Mapped["ExtractionRun | None"] = relationship(foreign_keys=[preview_run_id])
