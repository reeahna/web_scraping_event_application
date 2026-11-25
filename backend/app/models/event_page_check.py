"""Event pages the venue lookup read recently without finding a venue.

The event-page lookup (`app/extraction/event_page_fallback.py`) has a small
per-run page budget. Without a memory of pages it already read, a source
whose first pages state no venue spent that budget on the same pages every
day and never reached the rest. One row per (source, page URL) records the
last time the page was fetched successfully and named no usable venue; the
lookup skips such a page for its venue for a few days (a failed or blocked
fetch is never recorded, so it is retried on the next run).
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EventPageCheck(Base):
    __tablename__ = "event_page_checks"
    __table_args__ = (
        UniqueConstraint("website_id", "url_hash", name="uq_event_page_checks_website_url"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    website_id: Mapped[int] = mapped_column(
        ForeignKey("websites.id", ondelete="CASCADE"), index=True
    )
    url_hash: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(String(2000))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
