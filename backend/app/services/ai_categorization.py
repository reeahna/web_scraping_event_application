"""Background AI-categorization drain, run from the scheduler process.

Each call labels one batch of not-yet-AI-categorized events with Gemini and
commits. The scheduler ticks this on the same interval as its other drains, so
events added by the daily scrape pick up an AI category on their own without a
manual script run. It makes no network call when there is nothing to label and
raises GeminiQuotaExceeded (which the tick turns into a cooldown) when the free
tier is exhausted, so it never hammers the API.

Uses Groq when GROQ_API_KEY is set, else Gemini. Disabled unless one of the
two keys is configured and gemini_scheduled_enabled is on.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from sqlalchemy import and_, or_
from sqlalchemy.orm import Query, Session

from app.config import get_settings
from app.models.event import Event
from app.models.event_category import EventCategory
from app.services.ai_categorizer import (
    PROMPT_VERSION,
    CategoryOption,
    EventLabel,
    EventToClassify,
    GeminiCategorizer,
    GroqCategorizer,
)
from app.services.categorization import set_ai_category


def scheduled_categorization_enabled() -> bool:
    settings = get_settings()
    return bool(settings.groq_api_key or settings.gemini_api_key) and (
        settings.gemini_scheduled_enabled
    )


def provider_name() -> str | None:
    """Which AI provider categorization uses: Groq when its key is set, else
    Gemini when its key is set, else None."""
    settings = get_settings()
    if settings.groq_api_key:
        return "groq"
    if settings.gemini_api_key:
        return "gemini"
    return None


def batch_size() -> int:
    settings = get_settings()
    size = settings.groq_batch_size if provider_name() == "groq" else settings.gemini_batch_size
    return max(1, size)


def make_categorizer(
    *, min_interval_seconds: float | None = None, max_retries: int | None = None
) -> GeminiCategorizer:
    """A categorizer for the configured provider. Raises GeminiUnavailable when
    no key is set. min_interval_seconds and max_retries default to the
    provider's settings (the scheduler passes 0 for both)."""
    settings = get_settings()
    if provider_name() == "groq":
        return GroqCategorizer(
            settings.groq_api_key or "",
            settings.groq_model,
            timeout=settings.gemini_timeout_seconds,
            min_interval_seconds=(
                settings.groq_min_interval_seconds
                if min_interval_seconds is None
                else min_interval_seconds
            ),
            max_retries=settings.gemini_max_retries if max_retries is None else max_retries,
        )
    return GeminiCategorizer(
        settings.gemini_api_key or "",
        settings.gemini_model,
        timeout=settings.gemini_timeout_seconds,
        min_interval_seconds=(
            settings.gemini_min_interval_seconds
            if min_interval_seconds is None
            else min_interval_seconds
        ),
        max_retries=settings.gemini_max_retries if max_retries is None else max_retries,
        fallback_model=settings.gemini_fallback_model,
    )


def _category_options(db: Session) -> tuple[list[CategoryOption], dict[str, EventCategory]]:
    categories = db.query(EventCategory).filter(EventCategory.is_active.is_(True)).all()
    options = [
        CategoryOption(slug=c.slug, name=c.name, description=c.description) for c in categories
    ]
    return options, {c.slug: c for c in categories}


def categorizable_events(db: Session, *, today: date | None = None) -> Query:
    """The events worth spending AI quota on: visible ones that are upcoming or
    still running, soonest first, so a quota-limited run labels what students
    will see next before anything further out. Past, hidden, archived,
    confirmed-duplicate and undated events are left alone."""
    today = today or date.today()
    upcoming_or_ongoing = or_(
        and_(Event.end_date.isnot(None), Event.end_date >= today),
        and_(Event.end_date.is_(None), Event.start_date.isnot(None), Event.start_date >= today),
    )
    return (
        db.query(Event)
        .filter(
            Event.is_active.is_(True),
            Event.archived_at.is_(None),
            Event.is_recurrence_parent.is_(False),
            Event.duplicate_status != "confirmed_duplicate",
            upcoming_or_ongoing,
        )
        .order_by(Event.start_date.asc(), Event.id.asc())
    )


def pending_events(db: Session, *, today: date | None = None) -> Query:
    """Categorizable events (see above) still waiting for the AI: never
    AI-labeled, or labeled by an older prompt. A prompt change therefore
    re-checks them a batch at a time, resuming wherever the last run or the
    daily quota stopped."""
    outdated = or_(
        Event.category_source != "ai",
        Event.ai_prompt_version.is_(None),
        Event.ai_prompt_version != PROMPT_VERSION,
    )
    return categorizable_events(db, today=today).filter(outdated)


def apply_labels(
    events: list[Event], labels: dict[int, EventLabel], by_slug: dict[str, EventCategory]
) -> tuple[int, int]:
    """Write the model's labels onto the events, without committing. An event
    the model says a student would not go to (a finance seminar, a trade show)
    is also hidden when gemini_hide_unwanted is on, unless an administrator has
    already overridden its category, which means a person has looked at it.
    Hiding only clears is_active, so an administrator can bring one back.
    Returns (labeled, hidden)."""
    hide_unwanted = get_settings().gemini_hide_unwanted
    labeled = hidden = 0
    for event in events:
        label = labels.get(event.id)
        category = by_slug.get(label.category) if label else None
        if category is None:
            continue
        set_ai_category(event, category, PROMPT_VERSION)
        labeled += 1
        if (
            hide_unwanted
            and not label.keep
            and event.is_active
            and event.category_override_id is None
        ):
            event.is_active = False
            hidden += 1
    return labeled, hidden


def drain_categorization_queue(
    session_factory: Callable[[], Session], *, limit: int | None = None
) -> int:
    """Label up to one batch of un-AI-categorized events and commit. Returns the
    number labeled (0 when disabled or when nothing is pending — no API call is
    made in that case). Propagates GeminiQuotaExceeded so the caller can back
    off; other Gemini errors are left to the caller too."""
    if not scheduled_categorization_enabled():
        return 0
    size = limit or batch_size()

    db = session_factory()
    try:
        options, by_slug = _category_options(db)
        if not options:
            return 0
        events = pending_events(db).limit(size).all()
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
        with make_categorizer(min_interval_seconds=0.0, max_retries=0) as categorizer:
            labels = categorizer.classify_batch(options, to_classify)

        labeled, _hidden = apply_labels(events, labels, by_slug)
        db.commit()
        return labeled
    finally:
        db.close()
