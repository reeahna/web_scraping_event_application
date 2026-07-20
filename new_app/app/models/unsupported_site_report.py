from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import utcnow

if TYPE_CHECKING:
    from app.models.website import Website


class UnsupportedSiteReport(Base):
    """Recorded when no detector reaches the minimum confidence threshold,
    or the detection fetch itself was blocked. `fingerprint` deduplicates
    unchanged reports — a new row is only inserted when the underlying
    evidence actually changed (see app.repositories.unsupported_site_report
    .should_create_new_report)."""

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

    website: Mapped["Website"] = relationship()
