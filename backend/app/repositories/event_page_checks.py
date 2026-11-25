"""Pages the event-page venue lookup read recently without finding a venue.

See `app/models/event_page_check.py`. The memory lasts NO_VENUE_MEMORY_DAYS:
after that a page is read again, so a venue the organizer adds later is
still picked up.
"""

import hashlib
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.models.event_page_check import EventPageCheck

NO_VENUE_MEMORY_DAYS = 7


def _hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _cutoff(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)) - timedelta(days=NO_VENUE_MEMORY_DAYS)


def _aware(value: datetime) -> datetime:
    # SQLite hands timezone-aware columns back naive.
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def recent_no_venue_urls(
    db: Session, website_id: int, *, now: datetime | None = None
) -> frozenset[str]:
    """URLs of this source's event pages read without a venue in the last
    NO_VENUE_MEMORY_DAYS days."""
    cutoff = _cutoff(now)
    rows = db.query(EventPageCheck.url, EventPageCheck.checked_at).filter(
        EventPageCheck.website_id == website_id
    )
    return frozenset(url for url, checked in rows if _aware(checked) >= cutoff)


def record_no_venue_pages(
    db: Session, website_id: int, urls: Iterable[str], *, now: datetime | None = None
) -> None:
    """Remembers that these pages named no venue (as of `now`) and forgets
    this source's checks older than the memory window. Does not commit."""
    moment = now or datetime.now(UTC)
    cutoff = _cutoff(moment)
    fresh = {_hash(url): url for url in urls if url and len(url) <= 2000}
    existing = {
        row.url_hash: row
        for row in db.query(EventPageCheck).filter(EventPageCheck.website_id == website_id)
    }
    for key, row in existing.items():
        if key in fresh:
            row.checked_at = moment
        elif _aware(row.checked_at) < cutoff:
            db.delete(row)
    for key in sorted(fresh.keys() - existing.keys()):
        db.add(
            EventPageCheck(website_id=website_id, url_hash=key, url=fresh[key], checked_at=moment)
        )
