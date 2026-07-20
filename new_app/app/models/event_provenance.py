from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import utcnow

if TYPE_CHECKING:
    from app.models.event import Event
    from app.models.extraction_run import ExtractionRun
    from app.models.website import Website


class EventProvenance(Base):
    """Append-only: a later run adds a new row for the same event, never
    updates an existing one — this is what makes "retain prior provenance
    history" true by construction. Covers both event-level and field-level
    provenance (via `field_source_paths`) in one table rather than a second,
    fully-normalized field-per-row table that no query here actually needs.

    Never exposed on any public page — gated by the existing
    events.view_provenance permission in the admin UI only.
    """

    __tablename__ = "event_provenance"
    __table_args__ = (UniqueConstraint("event_id", "extraction_run_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    extraction_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="SET NULL"), index=True, default=None
    )
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True
    )
    source_page: Mapped[str] = mapped_column(String(2000))
    extraction_pattern: Mapped[str] = mapped_column(String(64))
    pattern_version: Mapped[str] = mapped_column(String(32))
    raw_record_hash: Mapped[str] = mapped_column(String(64))
    source_response_hash: Mapped[str] = mapped_column(String(64))
    field_source_paths: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    transformation_history: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    event: Mapped["Event"] = relationship()
    extraction_run: Mapped["ExtractionRun | None"] = relationship()
    website: Mapped["Website"] = relationship()
