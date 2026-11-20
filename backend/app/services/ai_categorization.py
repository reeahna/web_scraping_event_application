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

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Query, Session

from app.config import get_settings
from app.models.ai_label_batch import AiLabelBatch
from app.models.event import Event
from app.models.event_category import EventCategory
from app.services.ai_categorizer import (
    PROMPT_VERSION,
    CallStats,
    CategoryOption,
    EventLabel,
    EventToClassify,
    GeminiBadOutput,
    GeminiCategorizer,
    GeminiError,
    GeminiOverloaded,
    GeminiQuotaExceeded,
    GeminiTransportError,
    GroqCategorizer,
)
from app.services.ai_label_state import (
    NEEDS_REVIEW,
    NEEDS_REVIEW_RETRY_AFTER,
    STALE,
    record_label,
    record_unsuccessful_attempt,
    reset_for_retry,
)
from app.services.categorization import set_ai_category

logger = logging.getLogger(__name__)

# Batch records older than this are pruned by the drain.
BATCH_HISTORY = timedelta(days=30)


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
    will see next before anything further out. Past, hidden, cancelled,
    archived, confirmed-duplicate and undated events are left alone, and so are
    events whose category an administrator has set by hand: the override is
    what visitors see, and the AI never hides an overridden event, so asking
    about one would only spend tokens."""
    today = today or date.today()
    upcoming_or_ongoing = or_(
        and_(Event.end_date.isnot(None), Event.end_date >= today),
        and_(Event.end_date.is_(None), Event.start_date.isnot(None), Event.start_date >= today),
    )
    return (
        db.query(Event)
        .filter(
            Event.is_active.is_(True),
            Event.is_cancelled.is_not(True),
            Event.category_override_id.is_(None),
            Event.archived_at.is_(None),
            Event.is_recurrence_parent.is_(False),
            Event.duplicate_status != "confirmed_duplicate",
            upcoming_or_ongoing,
        )
        .order_by(Event.start_date.asc(), Event.id.asc())
    )


def _not_needs_review():
    return or_(Event.ai_label_status.is_(None), Event.ai_label_status != NEEDS_REVIEW)


def pending_events(db: Session, *, today: date | None = None) -> Query:
    """Categorizable events (see above) still waiting for the AI: never
    AI-labeled, labeled by an older prompt, or labeled before the words the
    model reads changed ("stale"). Events in needs-review are left out (see
    needs_review_events). A prompt change therefore re-checks them a batch at
    a time, resuming wherever the last run or the daily quota stopped.

    Order: events with no unsuccessful attempt first (new and changed events),
    then soonest first, so one hard event cannot hold up the rest."""
    outdated = or_(
        Event.category_source != "ai",
        Event.ai_prompt_version.is_(None),
        Event.ai_prompt_version != PROMPT_VERSION,
        Event.ai_label_status == STALE,
    )
    return (
        categorizable_events(db, today=today)
        .filter(outdated, _not_needs_review())
        .order_by(None)
        .order_by(
            case((Event.ai_attempts > 0, 1), else_=0),
            Event.start_date.asc(),
            Event.id.asc(),
        )
    )


def needs_review_events(db: Session, *, today: date | None = None) -> Query:
    """Categorizable events the AI could not label after AI_MAX_ATTEMPTS tries."""
    return categorizable_events(db, today=today).filter(Event.ai_label_status == NEEDS_REVIEW)


def needs_review_due(db: Session, *, now: datetime | None = None) -> Query:
    """Needs-review events whose last try is old enough for one more, used
    only when nothing else is waiting."""
    cutoff = (now or datetime.now(UTC)) - NEEDS_REVIEW_RETRY_AFTER
    return needs_review_events(db).filter(
        or_(Event.ai_last_attempt_at.is_(None), Event.ai_last_attempt_at <= cutoff)
    )


def retry_needs_review(db: Session, event_ids: list[int] | None = None) -> int:
    """Send needs-review events (all, or the given ids) back to the queue.
    Does not commit. Returns how many were re-queued."""
    query = db.query(Event).filter(Event.ai_label_status == NEEDS_REVIEW)
    if event_ids is not None:
        query = query.filter(Event.id.in_(event_ids))
    count = 0
    for event in query.all():
        reset_for_retry(event)
        count += 1
    return count


def queue_counts(db: Session, *, today: date | None = None) -> tuple[int, int]:
    """(waiting, labeled) among the events worth labeling: how many still wait
    for the AI and how many already carry a current AI label (needs-review
    events are neither). A few COUNT queries with no rows loaded, so it is
    cheap enough for the admin page."""
    waiting = pending_events(db, today=today).order_by(None).count()
    needs_review = needs_review_events(db, today=today).order_by(None).count()
    total = categorizable_events(db, today=today).order_by(None).count()
    return waiting, total - waiting - needs_review


def queue_status(db: Session, *, now: datetime | None = None) -> dict:
    """The AI label queue for monitoring: whether labeling is on, which
    provider, how many events wait, and how long the oldest has waited (by
    when it was first stored), in hours. Aggregates only, no rows loaded."""
    enabled = scheduled_categorization_enabled()
    status: dict = {"enabled": enabled, "provider": provider_name()}
    if not enabled:
        return status
    pending = pending_events(db).order_by(None)
    status["waiting"] = pending.count()
    status["needs_review"] = needs_review_events(db).order_by(None).count()
    usage = usage_summary(db, now=now)
    status["last_24h"] = {
        "requests": usage["requests"],
        "rate_limited": usage["rate_limited"],
        "events_labeled": usage["events_labeled"],
        "total_tokens": usage["total_tokens"],
    }
    oldest = pending.with_entities(func.min(Event.created_at)).scalar()
    if oldest is not None:
        if oldest.tzinfo is None:  # SQLite hands back naive UTC
            oldest = oldest.replace(tzinfo=UTC)
        hours = ((now or datetime.now(UTC)) - oldest).total_seconds() / 3600
        status["oldest_waiting_hours"] = round(max(hours, 0.0), 1)
    else:
        status["oldest_waiting_hours"] = None
    return status


def apply_labels(
    events: list[Event], labels: dict[int, EventLabel], by_slug: dict[str, EventCategory]
) -> tuple[int, int]:
    """Write the model's labels onto the events, without committing. An event
    the model says a student would not go to (a finance seminar, a trade show)
    is also hidden when gemini_hide_unwanted is on, unless an administrator has
    already overridden its category or set its visibility by hand, which means
    a person has looked at it.
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
        record_label(event)
        labeled += 1
        if (
            hide_unwanted
            and not label.keep
            and event.is_active
            and event.category_override_id is None
            and event.visibility_set_by_admin_at is None
        ):
            event.is_active = False
            hidden += 1
    return labeled, hidden


def _skip_reason(label: EventLabel | None) -> str:
    if label is None:
        return "the model left this event out of its answer"
    return f"the model answered with an unknown category {label.category[:40]!r}"


def _record_batch(
    db: Session,
    *,
    provider: str,
    stats: CallStats,
    outcome: str,
    submitted: int,
    labeled: int = 0,
    skipped: int = 0,
    failed: int = 0,
    hidden: int = 0,
) -> AiLabelBatch:
    row = AiLabelBatch(
        provider=provider,
        model=stats.model,
        outcome=outcome,
        http_status=stats.http_status,
        duration_ms=stats.duration_ms,
        events_submitted=submitted,
        events_labeled=labeled,
        events_skipped=skipped,
        events_failed=failed,
        events_hidden=hidden,
        prompt_tokens=stats.prompt_tokens,
        completion_tokens=stats.completion_tokens,
        total_tokens=stats.total_tokens,
        cached_tokens=stats.cached_tokens,
        reasoning_tokens=stats.reasoning_tokens,
        limit_requests=stats.limit_requests,
        remaining_requests=stats.remaining_requests,
        reset_requests=stats.reset_requests,
        limit_tokens=stats.limit_tokens,
        remaining_tokens=stats.remaining_tokens,
        reset_tokens=stats.reset_tokens,
        retry_after_seconds=stats.retry_after_seconds,
        limit_kind=stats.limit_kind,
        limit_value=stats.limit_value,
        limit_used=stats.limit_used,
        limit_requested=stats.limit_requested,
        error=stats.error,
    )
    db.add(row)
    # One structured line per request, so the same facts are in the logs.
    logger.info(
        "AI label batch: model=%s outcome=%s http=%s events=%d labeled=%d skipped=%d "
        "failed=%d hidden=%d tokens_in=%s tokens_out=%s tokens_total=%s cached=%s "
        "reasoning=%s remaining_requests=%s remaining_tokens=%s reset_tokens=%s "
        "retry_after=%s limit=%s limit_used=%s/%s requested=%s",
        row.model, outcome, row.http_status, submitted, labeled, skipped, failed, hidden,
        row.prompt_tokens, row.completion_tokens, row.total_tokens, row.cached_tokens,
        row.reasoning_tokens, row.remaining_requests, row.remaining_tokens, row.reset_tokens,
        row.retry_after_seconds, row.limit_kind, row.limit_used, row.limit_value,
        row.limit_requested,
    )
    return row


def prune_batch_history(db: Session, *, now: datetime | None = None) -> int:
    cutoff = (now or datetime.now(UTC)) - BATCH_HISTORY
    return (
        db.query(AiLabelBatch)
        .filter(AiLabelBatch.created_at < cutoff)
        .delete(synchronize_session=False)
    )


def usage_summary(db: Session, *, now: datetime | None = None, hours: int = 24) -> dict:
    """Totals over the last `hours` of labeling requests, for the admin page
    and /health/ready."""
    since = (now or datetime.now(UTC)) - timedelta(hours=hours)
    rows = db.query(
        func.count(AiLabelBatch.id),
        func.sum(case((AiLabelBatch.outcome == "rate_limited", 1), else_=0)),
        func.sum(case((AiLabelBatch.outcome == "ok", 1), else_=0)),
        func.coalesce(func.sum(AiLabelBatch.events_labeled), 0),
        func.coalesce(func.sum(AiLabelBatch.events_skipped), 0),
        func.coalesce(func.sum(AiLabelBatch.events_failed), 0),
        func.coalesce(func.sum(AiLabelBatch.events_hidden), 0),
        func.coalesce(func.sum(AiLabelBatch.prompt_tokens), 0),
        func.coalesce(func.sum(AiLabelBatch.completion_tokens), 0),
        func.coalesce(func.sum(AiLabelBatch.total_tokens), 0),
        func.coalesce(func.sum(AiLabelBatch.cached_tokens), 0),
    ).filter(AiLabelBatch.created_at >= since).one()
    (requests, rate_limited, ok, labeled, skipped, failed, hidden,
     prompt, completion, total, cached) = rows
    labeled = int(labeled or 0)
    total = int(total or 0)
    return {
        "hours": hours,
        "requests": int(requests or 0),
        "ok": int(ok or 0),
        "rate_limited": int(rate_limited or 0),
        "events_labeled": labeled,
        "events_skipped": int(skipped or 0),
        "events_failed": int(failed or 0),
        "events_hidden": int(hidden or 0),
        "prompt_tokens": int(prompt or 0),
        "completion_tokens": int(completion or 0),
        "total_tokens": total,
        "cached_tokens": int(cached or 0),
        "tokens_per_labeled_event": round(total / labeled) if labeled else None,
    }


def drain_categorization_queue(
    session_factory: Callable[[], Session], *, limit: int | None = None
) -> int:
    """Label up to one batch of waiting events and commit. Returns the number
    labeled (0 when disabled or when nothing is pending; no API call is made
    in that case). Every request is recorded in ai_label_batches.

    Per-event bookkeeping: a label resets the event's attempts; an answer that
    leaves an event out, names an unknown category, or cannot be parsed counts
    one unsuccessful attempt, and AI_MAX_ATTEMPTS of those move the event to
    needs-review instead of re-sending it forever. Rate limits, provider
    outages and network failures are not the events' fault and count nothing.
    Propagates GeminiQuotaExceeded (and other provider errors) so the caller
    can back off."""
    if not scheduled_categorization_enabled():
        return 0
    size = limit or batch_size()
    provider = provider_name() or "unknown"

    db = session_factory()
    try:
        options, by_slug = _category_options(db)
        if not options:
            return 0
        events = pending_events(db).limit(size).all()
        if not events:
            # Nothing new is waiting: give needs-review events their periodic
            # extra try, so an inconclusive answer is never final.
            events = needs_review_due(db).limit(size).all()
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
            try:
                labels = categorizer.classify_batch(options, to_classify)
            except GeminiQuotaExceeded:
                _record_batch(
                    db, provider=provider, stats=categorizer.last_stats,
                    outcome="rate_limited", submitted=len(events), failed=len(events),
                )
                db.commit()
                raise
            except (GeminiOverloaded, GeminiTransportError):
                _record_batch(
                    db, provider=provider, stats=categorizer.last_stats,
                    outcome="unavailable", submitted=len(events), failed=len(events),
                )
                db.commit()
                raise
            except GeminiBadOutput as exc:
                reason = f"unusable answer: {categorizer.last_stats.error or exc}"[:300]
                for event in events:
                    record_unsuccessful_attempt(event, reason)
                _record_batch(
                    db, provider=provider, stats=categorizer.last_stats,
                    outcome="error", submitted=len(events), failed=len(events),
                )
                db.commit()
                raise
            except GeminiError:
                _record_batch(
                    db, provider=provider, stats=categorizer.last_stats,
                    outcome="error", submitted=len(events), failed=len(events),
                )
                db.commit()
                raise
            stats = categorizer.last_stats

        labeled, hidden = apply_labels(events, labels, by_slug)
        skipped = 0
        for event in events:
            label = labels.get(event.id)
            if label is not None and label.category in by_slug:
                continue
            skipped += 1
            record_unsuccessful_attempt(event, _skip_reason(label))
        _record_batch(
            db, provider=provider, stats=stats, outcome="ok", submitted=len(events),
            labeled=labeled, skipped=skipped, hidden=hidden,
        )
        prune_batch_history(db)
        db.commit()
        return labeled
    finally:
        db.close()
