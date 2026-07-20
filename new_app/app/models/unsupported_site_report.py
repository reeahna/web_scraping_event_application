from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import utcnow

if TYPE_CHECKING:
    from app.models.extraction_run import ExtractionRun
    from app.models.user import User
    from app.models.website import Website


class UnsupportedSiteReport(Base):
    """Recorded when no detector reaches the minimum confidence threshold,
    or the detection fetch itself was blocked. `fingerprint` deduplicates
    unchanged reports — a new row is only inserted when the underlying
    evidence actually changed (see app.repositories.unsupported_site_report
    .should_create_new_report); an unchanged repeat instead bumps
    `occurrence_count`/`last_seen_at`/`latest_extraction_run_id` on this row
    (see .record_report_occurrence).

    `status` is validated against app.core.report_status.REPORT_STATUSES /
    ALLOWED_REPORT_TRANSITIONS — never an arbitrary string write.
    """

    __tablename__ = "unsupported_site_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True
    )
    submitted_url: Mapped[str] = mapped_column(String(2000))
    final_url: Mapped[str | None] = mapped_column(String(2000), default=None)
    http_status: Mapped[int | None] = mapped_column(Integer, default=None)
    page_title: Mapped[str | None] = mapped_column(String(500), default=None)
    detected_platform_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    available_detector_results: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    discovered_endpoints: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    browser_required: Mapped[bool] = mapped_column(Boolean, default=False)
    json_ld_presence: Mapped[bool] = mapped_column(Boolean, default=False)
    pagination_indicators: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    access_denied_or_challenge_detected: Mapped[bool] = mapped_column(Boolean, default=False)
    failure_reason: Mapped[str | None] = mapped_column(String(500), default=None)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    assigned_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    admin_notes: Mapped[str | None] = mapped_column(Text, default=None)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    occurrence_count: Mapped[int] = mapped_column(Integer, default=1)
    latest_extraction_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="SET NULL"), default=None
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    resolved_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    website: Mapped["Website"] = relationship()
    assigned_user: Mapped["User | None"] = relationship(foreign_keys=[assigned_user_id])
    resolved_by_user: Mapped["User | None"] = relationship(foreign_keys=[resolved_by_user_id])
    latest_extraction_run: Mapped["ExtractionRun | None"] = relationship()
