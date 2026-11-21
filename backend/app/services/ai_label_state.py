"""Per-event AI label bookkeeping, shared by the labeling drain and the import
pipeline.

* ``ai_input_hash`` is a digest of the words the model reads (title,
  description, source category, venue). It is stored whenever the AI is asked
  about an event, so a later re-scrape can tell whether those words changed.
* ``ai_label_status``: NULL until the AI has answered, then "labeled";
  "stale" when the words changed after a label, so the event is asked about
  again; "needs_review" after AI_MAX_ATTEMPTS answers in a row gave no usable
  label. An administrator can send a needs-review event back to the queue, a
  change to its words does so automatically, and the drain gives it one more
  try every NEEDS_REVIEW_RETRY_AFTER when nothing else is waiting, so no event
  is dropped for good.
* "provisional": only the backup model (GROQ_FALLBACK_MODEL) has labeled the
  event. Its category_source is then PROVISIONAL_SOURCE, never "ai", so
  nothing that reads AI labels takes it for a final one. It stays in the main
  model's queue until that model verifies it, and the backup never hides an
  event. Backup failures count in ai_fallback_attempts, never ai_attempts.

Kept free of HTTP and provider code so the import path can use it cheaply.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

LABELED = "labeled"
STALE = "stale"
NEEDS_REVIEW = "needs_review"
PROVISIONAL = "provisional"
# category_source of a category only the backup model has given.
PROVISIONAL_SOURCE = "ai_provisional"

# Unsuccessful answers in a row before an event stops being sent automatically.
AI_MAX_ATTEMPTS = 2
# Unusable backup-model answers before the backup stops asking about an event
# (it then simply waits for the main model, which it was doing anyway).
FALLBACK_MAX_ATTEMPTS = 1
NEEDS_REVIEW_RETRY_AFTER = timedelta(days=7)


def ai_input_hash(event: Any) -> str:
    """Digest of exactly the fields the categorizer sends for an event."""
    parts = [
        " ".join((getattr(event, name) or "").split())
        for name in ("title", "description", "source_category", "venue")
    ]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()


def requeue_if_inputs_changed(event: Any) -> bool:
    """Called after a re-scrape has written the event's fields. When the words
    the model reads differ from those it last saw, send the event back to the
    AI queue: a labeled event becomes "stale" (it keeps its current category
    until the new label arrives) and a needs-review event gets fresh attempts.
    An AI-labeled event from before hashes were stored gets its baseline hash
    and is not re-sent. Returns whether the event was re-queued."""
    current = ai_input_hash(event)
    if event.ai_input_hash is None:
        if event.category_source == "ai":
            event.ai_input_hash = current
        return False
    if event.ai_input_hash == current:
        return False
    if event.ai_label_status == NEEDS_REVIEW:
        event.ai_label_status = None
    elif event.category_source == "ai":
        event.ai_label_status = STALE
    # A provisional event stays provisional: it is already waiting for the
    # main model, which reads the new words when it gets there.
    event.ai_attempts = 0
    event.ai_fallback_attempts = 0
    event.ai_last_error = None
    return True


def record_label(
    event: Any, *, now: datetime | None = None, model: str | None = None
) -> None:
    """Bookkeeping for an event the main model just labeled (final)."""
    event.ai_label_status = LABELED
    event.ai_model = model[:100] if model else None
    event.ai_attempts = 0
    event.ai_fallback_attempts = 0
    event.ai_last_error = None
    event.ai_last_attempt_at = now or datetime.now(UTC)
    event.ai_input_hash = ai_input_hash(event)


def record_provisional_label(event: Any, *, model: str, now: datetime | None = None) -> None:
    """Bookkeeping for an event the backup model just gave a provisional
    category. ai_attempts is left as it is: it belongs to the main model."""
    event.ai_label_status = PROVISIONAL
    event.ai_model = model[:100]
    event.ai_last_attempt_at = now or datetime.now(UTC)
    event.ai_input_hash = ai_input_hash(event)


def record_fallback_failure(event: Any, reason: str, *, now: datetime | None = None) -> None:
    """The backup model gave the event no usable category. Nothing about the
    event's category, visibility or main-model attempts changes."""
    event.ai_fallback_attempts = (event.ai_fallback_attempts or 0) + 1
    event.ai_last_error = f"backup model: {reason}"[:300]
    event.ai_last_attempt_at = now or datetime.now(UTC)


def record_unsuccessful_attempt(
    event: Any, reason: str, *, now: datetime | None = None
) -> bool:
    """Bookkeeping for an answer that gave the event no usable label. Returns
    whether the event has now moved to needs-review."""
    event.ai_attempts = (event.ai_attempts or 0) + 1
    event.ai_last_error = reason[:300]
    event.ai_last_attempt_at = now or datetime.now(UTC)
    event.ai_input_hash = ai_input_hash(event)
    if event.ai_attempts >= AI_MAX_ATTEMPTS:
        event.ai_label_status = NEEDS_REVIEW
        return True
    return False


def reset_for_retry(event: Any) -> None:
    """An administrator asked for the event to be labeled again."""
    if event.category_source == "ai":
        event.ai_label_status = STALE
    elif event.category_source == PROVISIONAL_SOURCE:
        event.ai_label_status = PROVISIONAL
    else:
        event.ai_label_status = None
    event.ai_attempts = 0
    event.ai_fallback_attempts = 0
    event.ai_last_error = None
