from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.website import Website


class ExtractionRun(Base):
    """One row per detect/preview/manual extraction invocation. Append-only
    history — no updated_at; `completed_at` is set once, when the run ends."""

    __tablename__ = "extraction_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True
    )
    configuration_version: Mapped[int | None] = mapped_column(Integer, default=None)
    pattern_name: Mapped[str | None] = mapped_column(String(64), default=None)
    run_type: Mapped[str] = mapped_column(String(16))  # detection | preview | manual | scheduled
    status: Mapped[str] = mapped_column(String(16), index=True)
    source_url: Mapped[str] = mapped_column(String(2000))
    final_url: Mapped[str | None] = mapped_column(String(2000), default=None)

    events_found: Mapped[int] = mapped_column(Integer, default=0)
    events_valid: Mapped[int] = mapped_column(Integer, default=0)
    events_rejected: Mapped[int] = mapped_column(Integer, default=0)
    events_inserted: Mapped[int] = mapped_column(Integer, default=0)
    events_updated: Mapped[int] = mapped_column(Integer, default=0)
    duplicates_skipped: Mapped[int] = mapped_column(Integer, default=0)

    # JSON-encoded diagnostics — never secrets, never unbounded raw response
    # bodies (see app/extraction/fetch.py's byte cap and body_hash-only
    # retention).
    warnings: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    error_summary: Mapped[str | None] = mapped_column(String(1000), default=None)
    detector_evidence: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    response_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    initiating_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)

    website: Mapped["Website"] = relationship()
    initiating_user: Mapped["User | None"] = relationship()
