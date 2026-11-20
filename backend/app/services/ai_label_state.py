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

# Unsuccessful answers in a row before an event stops being sent automatically.
AI_MAX_ATTEMPTS = 2
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
    event.ai_attempts = 0
    event.ai_last_error = None
    return True


def record_label(event: Any, *, now: datetime | None = None) -> None:
    """Bookkeeping for an event the AI just labeled."""
    event.ai_label_status = LABELED
    event.ai_attempts = 0
    event.ai_last_error = None
    event.ai_last_attempt_at = now or datetime.now(UTC)
    event.ai_input_hash = ai_input_hash(event)


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
    event.ai_label_status = STALE if event.category_source == "ai" else None
    event.ai_attempts = 0
    event.ai_last_error = None
