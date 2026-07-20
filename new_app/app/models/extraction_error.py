from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import utcnow

if TYPE_CHECKING:
    from app.models.extraction_run import ExtractionRun


class ExtractionError(Base):
    """Structured, safe-to-display error records for one extraction run.
    `safe_message` is never a raw exception string or response body — never
    stores secrets or full unsafe response bodies."""

    __tablename__ = "extraction_errors"

    id: Mapped[int] = mapped_column(primary_key=True)
    extraction_run_id: Mapped[int] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[str] = mapped_column(String(32), index=True)
    error_code: Mapped[str] = mapped_column(String(64), index=True)
    safe_message: Mapped[str] = mapped_column(String(500))
    candidate_index: Mapped[int | None] = mapped_column(Integer, default=None)
    field_name: Mapped[str | None] = mapped_column(String(255), default=None)
    source_page: Mapped[str | None] = mapped_column(String(2000), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    extraction_run: Mapped["ExtractionRun"] = relationship()
