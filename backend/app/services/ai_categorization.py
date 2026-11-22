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
    ProviderAccessBlocked,
    SplitPrompt,
)
from app.services.ai_label_state import (
    FALLBACK_MAX_ATTEMPTS,
    NEEDS_REVIEW,
    NEEDS_REVIEW_RETRY_AFTER,
    PROVISIONAL,
    PROVISIONAL_SOURCE,
    STALE,
    record_fallback_failure,
    record_label,
    record_provisional_label,
    record_unsuccessful_attempt,
    reset_for_retry,
)
from app.services.categorization import set_ai_category, set_provisional_category

logger = logging.getLogger(__name__)

# Batch records older than this are pruned by the drain.
BATCH_HISTORY = timedelta(days=30)
# ai_label_batches.outcome of a request the provider's spend limit refused.
BLOCKED = "blocked"
# Outcomes after which a model waits before it is asked again.
WAITING_OUTCOMES = ("rate_limited", BLOCKED)


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
    *,
    min_interval_seconds: float | None = None,
    max_retries: int | None = None,
    model: str | None = None,
) -> GeminiCategorizer:
    """A categorizer for the configured provider. Raises GeminiUnavailable when
    no key is set. min_interval_seconds and max_retries default to the
    provider's settings (the scheduler passes 0 for both); model defaults to
    the provider's primary model."""
    settings = get_settings()
    if provider_name() == "groq":
        return GroqCategorizer(
            settings.groq_api_key or "",
            model or settings.groq_model,
            description_chars=settings.ai_description_chars,
            prompt_layout=settings.groq_prompt_layout,
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
        description_chars=settings.ai_description_chars,
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
    model reads changed ("stale"). That includes every event with only a
    provisional backup-model label (category_source is not "ai"): those wait
    here for the main model to verify them like any other. Events in
    needs-review are left out (see needs_review_events). A prompt change
    therefore re-checks them a batch at a time, resuming wherever the last run
    or the daily quota stopped.

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


def provisional_events(db: Session, *, today: date | None = None) -> Query:
    """Categorizable events whose category so far comes only from the backup
    model, waiting for the main model to verify it."""
    return categorizable_events(db, today=today).filter(
        Event.category_source == PROVISIONAL_SOURCE
    )


def fallback_pending_events(db: Session, *, today: date | None = None) -> Query:
    """The events the backup model may label: waiting events that have never
    had a main-model AI label (so no AI label is ever replaced), have no
    provisional label yet (so the backup answers once per event, not again
    and again), are not in needs-review and have not used up their backup
    tries. Soonest first, so the events visitors see next get a category
    first. See may_take_provisional_label for the same rule per event."""
    return (
        categorizable_events(db, today=today)
        .filter(
            Event.category_source.not_in(("ai", PROVISIONAL_SOURCE)),
            _not_needs_review(),
            or_(
                Event.ai_label_status.is_(None),
                Event.ai_label_status.not_in((STALE, PROVISIONAL)),
            ),
            or_(
                Event.ai_fallback_attempts.is_(None),
                Event.ai_fallback_attempts < FALLBACK_MAX_ATTEMPTS,
            ),
        )
        .order_by(None)
        .order_by(Event.start_date.asc(), Event.id.asc())
    )


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
    for the main model and how many already carry a current, final AI label
    (needs-review events are neither). Provisional backup-model labels count
    as waiting, never as labeled. A few COUNT queries with no rows loaded, so it is
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
    # Part of "waiting": categorized by the backup model only, not yet verified.
    status["provisional"] = provisional_events(db).order_by(None).count()
    # New events reaching the queue: the main model keeps up only while it
    # labels more than this a day.
    since = (now or datetime.now(UTC)) - timedelta(hours=24)
    status["new_last_24h"] = (
        categorizable_events(db).order_by(None).filter(Event.created_at >= since).count()
    )
    usage = usage_summary(db, now=now)
    status["last_24h"] = {
        "requests": usage["requests"],
        "rate_limited": usage["rate_limited"],
        "blocked": usage["blocked"],
        "events_labeled": usage["events_labeled"],
        "events_provisional": usage["events_provisional"],
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
    events: list[Event],
    labels: dict[int, EventLabel],
    by_slug: dict[str, EventCategory],
    *,
    model: str | None = None,
) -> tuple[int, int]:
    """Write the main model's labels onto the events, without committing. An
    event the model says a student would not go to (a finance seminar, a trade
    show) is also hidden when gemini_hide_unwanted is on, unless an
    administrator has already overridden its category or set its visibility by
    hand, which means a person has looked at it.
    Hiding only clears is_active, so an administrator can bring one back.
    These labels are final: one replaces a provisional backup-model label.
    Returns (labeled, hidden)."""
    hide_unwanted = get_settings().gemini_hide_unwanted
    labeled = hidden = 0
    for event in events:
        label = labels.get(event.id)
        category = by_slug.get(label.category) if label else None
        if category is None:
            continue
        set_ai_category(event, category, PROMPT_VERSION)
        record_label(event, model=model)
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


def may_take_provisional_label(event: Event) -> bool:
    """Whether the backup model may give `event` a provisional category right
    now: only an event that has no main-model AI label (current or older), no
    provisional label already, no admin override, and is still visible. This
    is checked again just before writing, so a label or override that arrived
    while the request was in flight always wins."""
    return (
        event.category_source not in ("ai", PROVISIONAL_SOURCE)
        and event.category_override_id is None
        and event.is_active
        and event.ai_label_status not in (NEEDS_REVIEW, STALE, PROVISIONAL)
    )


def apply_provisional_labels(
    events: list[Event],
    labels: dict[int, EventLabel],
    by_slug: dict[str, EventCategory],
    *,
    model: str,
) -> tuple[int, int, int]:
    """Write the backup model's answers as provisional categories, without
    committing. The backup never hides anything: its keep/hide answer is
    ignored, and visibility is left exactly as it was. An event that may no
    longer take a provisional label (see may_take_provisional_label) is left
    untouched. Returns (labeled, unusable, passed_over): unusable answers count
    one backup failure on the event; passed-over events count nothing."""
    labeled = unusable = passed_over = 0
    for event in events:
        if not may_take_provisional_label(event):
            passed_over += 1
            continue
        label = labels.get(event.id)
        category = by_slug.get(label.category) if label else None
        if category is None:
            unusable += 1
            record_fallback_failure(event, _skip_reason(label))
            continue
        set_provisional_category(event, category)
        record_provisional_label(event, model=model)
        labeled += 1
    return labeled, unusable, passed_over


def estimate_tokens(text: str) -> int:
    """A deliberately high token estimate (3.5 characters a token) for
    keeping requests under a per-minute token limit."""
    return int(len(text) / 3.5) + 1


def fit_to_token_budget(
    categorizer: GeminiCategorizer,
    options: list[CategoryOption],
    events: list[EventToClassify],
    max_tokens: int,
) -> list[EventToClassify]:
    """The longest leading run of `events` whose prompt is estimated at or
    under max_tokens (always at least one event)."""
    def size(items: list[EventToClassify]) -> int:
        prompt = categorizer._build_prompt(options, items)
        if isinstance(prompt, SplitPrompt):
            return estimate_tokens(prompt.instructions + prompt.events)
        return estimate_tokens(prompt)

    fitted = list(events)
    while len(fitted) > 1 and size(fitted) > max_tokens:
        fitted.pop()
    return fitted


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
    provisional: bool = False,
) -> AiLabelBatch:
    row = AiLabelBatch(
        provider=provider,
        model=stats.model,
        provisional=provisional,
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
        "AI label batch: model=%s provisional=%s outcome=%s http=%s events=%d labeled=%d "
        "skipped=%d failed=%d hidden=%d tokens_in=%s tokens_out=%s tokens_total=%s cached=%s "
        "reasoning=%s remaining_requests=%s remaining_tokens=%s reset_tokens=%s "
        "retry_after=%s limit=%s limit_used=%s/%s requested=%s",
        row.model, provisional, outcome, row.http_status, submitted, labeled, skipped,
        failed, hidden,
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
    and /health/ready. events_labeled counts final main-model labels only;
    backup-model labels are events_provisional. Tokens cover both models."""
    since = (now or datetime.now(UTC)) - timedelta(hours=hours)
    rows = db.query(
        func.count(AiLabelBatch.id),
        func.sum(case((AiLabelBatch.outcome == "rate_limited", 1), else_=0)),
        func.sum(case((AiLabelBatch.outcome == "ok", 1), else_=0)),
        func.sum(case((AiLabelBatch.outcome == BLOCKED, 1), else_=0)),
        func.coalesce(
            func.sum(case((AiLabelBatch.provisional.is_(True), 0),
                          else_=AiLabelBatch.events_labeled)), 0),
        func.coalesce(
            func.sum(case((AiLabelBatch.provisional.is_(True), AiLabelBatch.events_labeled),
                          else_=0)), 0),
        func.coalesce(func.sum(AiLabelBatch.events_skipped), 0),
        func.coalesce(func.sum(AiLabelBatch.events_failed), 0),
        func.coalesce(func.sum(AiLabelBatch.events_hidden), 0),
        func.coalesce(func.sum(AiLabelBatch.prompt_tokens), 0),
        func.coalesce(func.sum(AiLabelBatch.completion_tokens), 0),
        func.coalesce(func.sum(AiLabelBatch.total_tokens), 0),
        func.coalesce(func.sum(AiLabelBatch.cached_tokens), 0),
    ).filter(AiLabelBatch.created_at >= since).one()
    (requests, rate_limited, ok, blocked, labeled, provisional, skipped, failed, hidden,
     prompt, completion, total, cached) = rows
    labeled = int(labeled or 0)
    total = int(total or 0)
    return {
        "hours": hours,
        "requests": int(requests or 0),
        "ok": int(ok or 0),
        "rate_limited": int(rate_limited or 0),
        "blocked": int(blocked or 0),
        "events_labeled": labeled,
        "events_provisional": int(provisional or 0),
        "events_skipped": int(skipped or 0),
        "events_failed": int(failed or 0),
        "events_hidden": int(hidden or 0),
        "prompt_tokens": int(prompt or 0),
        "completion_tokens": int(completion or 0),
        "total_tokens": total,
        "cached_tokens": int(cached or 0),
        "tokens_per_labeled_event": (
            round(total / (labeled + int(provisional or 0)))
            if labeled + int(provisional or 0) else None
        ),
    }


def model_cooldown_until(
    db: Session, model: str, *, now: datetime | None = None
) -> datetime | None:
    """When `model` may be asked again, from its latest request when that was
    rate limited or blocked by the provider's spend limit (stored, so a
    scheduler restart remembers it). None when it is free now.
    Each model's quota is tracked on its own."""
    now = now or datetime.now(UTC)
    row = (
        db.query(AiLabelBatch)
        .filter(AiLabelBatch.model == model)
        .order_by(AiLabelBatch.created_at.desc(), AiLabelBatch.id.desc())
        .first()
    )
    if row is None or row.outcome not in WAITING_OUTCOMES:
        return None
    created = row.created_at
    if created.tzinfo is None:  # SQLite hands back naive UTC
        created = created.replace(tzinfo=UTC)
    until = created + timedelta(seconds=(row.retry_after_seconds or 60.0) + 30.0)
    return until if until > now else None


def label_models() -> list[str]:
    """The models labeling may use, in order: the main model, then the backup
    (groq_fallback_model) when one is set. Groq only; the backup is off unless
    configured."""
    settings = get_settings()
    if provider_name() != "groq":
        return [settings.gemini_model]
    models = [settings.groq_model]
    fallback = settings.groq_fallback_model
    if fallback and fallback != settings.groq_model:
        models.append(fallback)
    return models


def drain_categorization_queue(
    session_factory: Callable[[], Session],
    *,
    limit: int | None = None,
    now: datetime | None = None,
) -> int:
    """Label up to one batch of waiting events and commit. Returns the number
    labeled (0 when disabled or when nothing is pending; no API call is made
    in that case). Every request is recorded in ai_label_batches.

    Models: the main model is always asked first. Only while it is rate
    limited (its own free quota is used up), and only when a backup model is
    configured, the same tick asks the backup instead. The backup's labels are
    provisional (see label_with_backup); the next tick goes back to the main
    model as soon as its wait is over. A model whose wait is not over is never
    asked. When every usable model is waiting, or the backup has nothing it is
    allowed to label, this raises GeminiQuotaExceeded with the shortest wait,
    so the scheduler sleeps until then. Provider outages and network failures
    on the main model never switch to the backup.

    Per-event bookkeeping for the main model: a label resets the event's
    attempts; an answer that leaves an event out, names an unknown category,
    or cannot be parsed counts one unsuccessful attempt, and AI_MAX_ATTEMPTS
    of those move the event to needs-review instead of re-sending it forever.
    Rate limits, provider outages and network failures are not the events'
    fault and count nothing. Other provider errors propagate so the caller can
    back off."""
    if not scheduled_categorization_enabled():
        return 0
    now = now or datetime.now(UTC)
    size = limit or batch_size()
    main, *backups = label_models()

    db = session_factory()
    try:
        options, by_slug = _category_options(db)
        if not options:
            return 0
        main_wait = model_cooldown_until(db, main, now=now)
        if main_wait is None:
            try:
                return _label_batch(db, options, by_slug, main, size)
            except GeminiQuotaExceeded as exc:
                if not backups:
                    raise
                main_wait = now + timedelta(seconds=(exc.retry_after_seconds or 60.0) + 30.0)
        if not backups:
            raise GeminiQuotaExceeded(
                "the labeling model is waiting for its rate limit",
                max((main_wait - now).total_seconds(), 1.0), None,
            )
        waits = [main_wait]
        backup = backups[0]
        backup_wait = model_cooldown_until(db, backup, now=now)
        if backup_wait is None:
            try:
                labeled = label_with_backup(db, options, by_slug, backup, size)
                if labeled is not None:
                    return labeled
            except GeminiQuotaExceeded as exc:
                backup_wait = now + timedelta(seconds=(exc.retry_after_seconds or 60.0) + 30.0)
            except GeminiError as exc:
                # The backup failed (timeout, outage, unusable answer). Nothing
                # was written to any event; already recorded. Don't let its
                # error back-off delay the main model: just wait for the main
                # model's own quota.
                logger.warning("backup labeling model failed: %s", exc)
        if backup_wait is not None:
            waits.append(backup_wait)
        # Nothing the backup may label, or it is waiting too: sleep until the
        # first model is free again.
        seconds = max((min(waits) - now).total_seconds(), 1.0)
        raise GeminiQuotaExceeded(
            "every labeling model is waiting for its rate limit", seconds, None
        )
    finally:
        db.close()


def _classify(
    db: Session,
    options: list[CategoryOption],
    model: str,
    events: list[Event],
    *,
    provisional: bool,
) -> tuple[list[Event], dict[int, EventLabel], CallStats]:
    """One request to `model` about `events`. Records a failed request in
    ai_label_batches (and commits that record) before re-raising; on success
    returns the events actually sent (after optional token trimming), the
    labels and the call's stats. Touches no event."""
    provider = provider_name() or "unknown"
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
    # per tick already stays inside the rate limits. Fail fast on a 429 (0
    # retries) so the model's cooldown kicks in instead of burning quota.
    with make_categorizer(min_interval_seconds=0.0, max_retries=0, model=model) as categorizer:
        max_tokens = get_settings().groq_max_prompt_tokens
        if provider == "groq" and max_tokens > 0:
            to_classify = fit_to_token_budget(categorizer, options, to_classify, max_tokens)
            kept = {item.id for item in to_classify}
            events = [event for event in events if event.id in kept]
        try:
            labels = categorizer.classify_batch(options, to_classify)
        except GeminiError as exc:
            if isinstance(exc, ProviderAccessBlocked):
                # A spend-limit block: billing, not the events. Counted as
                # nothing against them (only GeminiBadOutput is).
                outcome = BLOCKED
            elif isinstance(exc, GeminiQuotaExceeded):
                outcome = "rate_limited"
            elif isinstance(exc, GeminiOverloaded | GeminiTransportError):
                outcome = "unavailable"
            else:
                outcome = "error"
            if isinstance(exc, GeminiBadOutput):
                reason = f"unusable answer: {categorizer.last_stats.error or exc}"[:300]
                for event in events:
                    if provisional:
                        record_fallback_failure(event, reason)
                    else:
                        record_unsuccessful_attempt(event, reason)
            _record_batch(
                db, provider=provider, stats=categorizer.last_stats, outcome=outcome,
                submitted=len(events), failed=len(events), provisional=provisional,
            )
            db.commit()
            raise
        return events, labels, categorizer.last_stats


def _label_batch(
    db: Session,
    options: list[CategoryOption],
    by_slug: dict[str, EventCategory],
    model: str,
    size: int,
) -> int:
    """One main-model batch: final labels, which may hide events."""
    provider = provider_name() or "unknown"
    events = pending_events(db).limit(size).all()
    if not events:
        # Nothing new is waiting: give needs-review events their periodic
        # extra try, so an inconclusive answer is never final.
        events = needs_review_due(db).limit(size).all()
    if not events:
        return 0

    events, labels, stats = _classify(db, options, model, events, provisional=False)
    labeled, hidden = apply_labels(events, labels, by_slug, model=model)
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


def label_with_backup(
    db: Session,
    options: list[CategoryOption],
    by_slug: dict[str, EventCategory],
    model: str,
    size: int,
) -> int | None:
    """One backup-model batch, used only while the main model is rate
    limited. Returns how many events got a provisional category, or None when
    no event may be labeled by the backup (nothing is sent then).

    Safeguards: only events that have never had a main-model label and have
    no admin override are sent (fallback_pending_events), and each is checked
    again before writing; the answer's keep/hide is ignored, so no event is
    hidden; the category is stored as provisional and the event stays in the
    main model's queue; an unusable answer or a failed request changes no
    category, no visibility and no main-model attempt. Commits."""
    provider = provider_name() or "unknown"
    events = fallback_pending_events(db).limit(size).all()
    if not events:
        return None
    events, labels, stats = _classify(db, options, model, events, provisional=True)
    # Re-read each event: a main-model label, an admin override or a hide that
    # landed while the request was in flight must win over this answer.
    for event in events:
        db.refresh(event)
    labeled, unusable, _passed_over = apply_provisional_labels(
        events, labels, by_slug, model=model
    )
    _record_batch(
        db, provider=provider, stats=stats, outcome="ok", submitted=len(events),
        labeled=labeled, skipped=unusable, hidden=0, provisional=True,
    )
    prune_batch_history(db)
    db.commit()
    return labeled
