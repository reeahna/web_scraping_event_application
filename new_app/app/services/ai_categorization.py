"""Background AI-categorization drain, run from the scheduler process.

Each call labels one batch of not-yet-AI-categorized events with Gemini and
commits. The scheduler ticks this on the same interval as its other drains, so
events added by the daily scrape pick up an AI category on their own without a
manual script run. It makes no network call when there is nothing to label and
raises GeminiQuotaExceeded (which the tick turns into a cooldown) when the free
tier is exhausted, so it never hammers the API.

Disabled unless a key is configured and gemini_scheduled_enabled is on.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.event import Event
from app.models.event_category import EventCategory
from app.services.ai_categorizer import (
    CategoryOption,
    EventToClassify,
    GeminiCategorizer,
)
from app.services.categorization import set_ai_category


def scheduled_categorization_enabled() -> bool:
    settings = get_settings()
    return bool(settings.gemini_api_key) and settings.gemini_scheduled_enabled


def _category_options(db: Session) -> tuple[list[CategoryOption], dict[str, EventCategory]]:
    categories = db.query(EventCategory).filter(EventCategory.is_active.is_(True)).all()
    options = [
        CategoryOption(slug=c.slug, name=c.name, description=c.description) for c in categories
    ]
    return options, {c.slug: c for c in categories}


def drain_categorization_queue(
    session_factory: Callable[[], Session], *, limit: int | None = None
) -> int:
    """Label up to one batch of un-AI-categorized events and commit. Returns the
    number labeled (0 when disabled or when nothing is pending — no API call is
    made in that case). Propagates GeminiQuotaExceeded so the caller can back
    off; other Gemini errors are left to the caller too."""
    settings = get_settings()
    if not scheduled_categorization_enabled():
        return 0
    batch_size = limit or max(1, settings.gemini_batch_size)

    db = session_factory()
    try:
        options, by_slug = _category_options(db)
        if not options:
            return 0
        events = (
            db.query(Event)
            .filter(Event.category_source != "ai")
            .order_by(Event.id)
            .limit(batch_size)
            .all()
        )
        if not events:
            return 0

        to_classify = [
            EventToClassify(
                id=event.id,
                title=event.title,
                description=event.description,
                source_category=event.source_category,
                venue=event.venue,
            )
            for event in events
        ]
        # No internal throttle: the scheduler spaces ticks out, so one request
        # per tick already stays inside the rate limit. Fail fast on a 429 (0
        # retries) so the tick's cooldown kicks in instead of burning quota.
        with GeminiCategorizer(
            settings.gemini_api_key,
            settings.gemini_model,
            timeout=settings.gemini_timeout_seconds,
            min_interval_seconds=0.0,
            max_retries=0,
        ) as categorizer:
            labels = categorizer.classify_batch(options, to_classify)

        labeled = 0
        for event in events:
            slug = labels.get(event.id)
            category = by_slug.get(slug) if slug else None
            if category is None:
                continue
            set_ai_category(event, category)
            labeled += 1
        db.commit()
        return labeled
    finally:
        db.close()
