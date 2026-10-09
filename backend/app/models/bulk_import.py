"""Durable tracking for an administrator's "import all active websites" run.

A `BulkImportRun` is a header; each `BulkImportItem` records one website's
outcome. These only *track* the operation — the extraction itself always goes
through the ordinary `run_extraction` path, so approved-config version,
HTTP/browser strategy, normalization, dedup, persistence and per-website
ExtractionRun history are all identical to a normal import.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.models.user import User

# Header statuses.
BULK_QUEUED = "queued"
BULK_RUNNING = "running"
BULK_COMPLETED = "completed"
BULK_COMPLETED_WITH_FAILURES = "completed_with_failures"
BULK_FAILED = "failed"

# Per-item statuses.
ITEM_QUEUED = "queued"
ITEM_RUNNING = "running"
ITEM_SUCCESS = "success"
ITEM_PARTIAL = "partial"
ITEM_FAILED = "failed"
ITEM_BLOCKED = "blocked"
ITEM_SKIPPED_ALREADY_RUNNING = "skipped_already_running"
ITEM_SKIPPED_INELIGIBLE = "skipped_ineligible"

_TERMINAL_HEADER = frozenset({BULK_COMPLETED, BULK_COMPLETED_WITH_FAILURES, BULK_FAILED})


class BulkImportRun(Base):
    __tablename__ = "bulk_import_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    requested_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    status: Mapped[str] = mapped_column(String(32), default=BULK_QUEUED, index=True)
    # Counts computed at planning time (how the run was scoped).
    eligible_count: Mapped[int] = mapped_column(Integer, default=0)
    http_count: Mapped[int] = mapped_column(Integer, default=0)
    browser_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    already_running_count: Mapped[int] = mapped_column(Integer, default=0)

    requested_by: Mapped["User | None"] = relationship("User")
    items: Mapped[list["BulkImportItem"]] = relationship(
        "BulkImportItem", back_populates="run", cascade="all, delete-orphan",
        order_by="BulkImportItem.id",
    )

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_HEADER

    @property
    def completed_count(self) -> int:
        return sum(1 for i in self.items if i.status not in (ITEM_QUEUED, ITEM_RUNNING))


class BulkImportItem(Base):
    __tablename__ = "bulk_import_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    bulk_run_id: Mapped[int] = mapped_column(
        ForeignKey("bulk_import_runs.id", ondelete="CASCADE"), index=True
    )
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True
    )
    website_name: Mapped[str] = mapped_column(String(255))
    execution_strategy: Mapped[str] = mapped_column(String(16), default="http")  # http | browser
    status: Mapped[str] = mapped_column(String(32), default=ITEM_QUEUED, index=True)
    events_found: Mapped[int] = mapped_column(Integer, default=0)
    events_valid: Mapped[int] = mapped_column(Integer, default=0)
    events_inserted: Mapped[int] = mapped_column(Integer, default=0)
    events_updated: Mapped[int] = mapped_column(Integer, default=0)
    duplicates_skipped: Mapped[int] = mapped_column(Integer, default=0)
    extraction_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="SET NULL"), default=None
    )
    error_summary: Mapped[str | None] = mapped_column(String(500), default=None)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    run: Mapped["BulkImportRun"] = relationship("BulkImportRun", back_populates="items")
