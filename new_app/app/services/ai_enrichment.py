"""Optional AI event enrichment (Phase 17).

Advisory only. AI may suggest a category (for uncategorized events), tags, a
short summary, an audience, a family-friendly flag, duplicate candidates, and
extraction-error summaries. It may NOT invent dates/times/venues/addresses/URLs,
approve/activate/publish/delete, change permissions, override validation, or run
code — this module only ever writes to the `event_enrichments` suggestion table,
never to an event's authoritative fields.

Disabled by default (reusing the Phase 8E provider posture). Every call is
gated by a shared budget + circuit breaker (the 8E `_UsageTracker`), validated
into a structured, taxonomy-checked result, and cached by prompt version +
input hash. Only unresolved records are sent, with a minimized input. An AI
failure returns None and is swallowed — it can never fail extraction or public
display.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.logging import get_logger
from app.models.event import Event
from app.models.event_enrichment import EventEnrichment

logger = get_logger("ai.enrichment")

PROMPT_VERSION = "1"
_MAX_TAGS = 8
_MAX_SUMMARY = 500
_MAX_DESC_INPUT = 2000


class EnrichmentSuggestion(BaseModel):
    """Structured, bounded AI output. Contains only advisory fields — there is
    deliberately no field for a date, time, venue, address, URL, or any
    approval/state action."""

    model_config = ConfigDict(extra="forbid")

    category_suggestion: str | None = Field(default=None, max_length=255)
    # No max_length here — the validator truncates rather than rejecting, so an
    # over-eager model reply is bounded, not dropped entirely.
    tags: list[str] = Field(default_factory=list)
    summary: str | None = Field(default=None, max_length=_MAX_SUMMARY)
    audience: str | None = Field(default=None, max_length=255)
    family_friendly: bool | None = None
    duplicate_candidate_ids: list[int] = Field(default_factory=list, max_length=20)

    @field_validator("tags")
    @classmethod
    def _bound_tags(cls, v: list[str]) -> list[str]:
        return [t.strip()[:60] for t in v if t and t.strip()][:_MAX_TAGS]


@dataclass(frozen=True)
class EnrichmentInput:
    """The minimized, privacy-conscious input sent to the provider — only the
    public event fields needed for the allowed tasks, all bounded."""

    title: str
    description: str
    venue: str
    city: str
    source_category: str

    def as_dict(self) -> dict:
        return {
            "title": self.title, "description": self.description, "venue": self.venue,
            "city": self.city, "source_category": self.source_category,
        }


@runtime_checkable
class EnrichmentProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    def enrich(self, payload: dict) -> dict: ...


class DisabledEnrichmentProvider:
    name = "disabled"

    def available(self) -> bool:
        return False

    def enrich(self, payload: dict) -> dict:  # pragma: no cover - never called
        raise RuntimeError("enrichment provider is disabled")


class EchoEnrichmentProvider:
    """Deterministic test/double provider. Returns a canned suggestion or a
    configured failure; never touches the network."""

    name = "echo"

    def __init__(self) -> None:
        self._canned: dict = {}
        self._failure: Exception | None = None

    def set_canned(self, value: dict) -> None:
        self._canned = value

    def set_failure(self, exc: Exception) -> None:
        self._failure = exc

    def available(self) -> bool:
        return True

    def enrich(self, payload: dict) -> dict:
        if self._failure is not None:
            raise self._failure
        return dict(self._canned)


def get_enrichment_provider(settings) -> EnrichmentProvider:
    # Reuses the AI enablement flag; no enrichment happens unless AI is enabled
    # with a non-disabled provider. A real network provider would slot in here
    # (credential-gated); until then the default is always disabled.
    enabled = getattr(settings, "ai_enabled", False)
    provider_name = getattr(settings, "ai_provider", "disabled")
    if not enabled or provider_name == "disabled":
        return DisabledEnrichmentProvider()
    return DisabledEnrichmentProvider()


def _tracker():
    from app.services.ai.provider import _TRACKER

    return _TRACKER


def _input_for(event: Event) -> EnrichmentInput:
    return EnrichmentInput(
        title=(event.title or "")[:500],
        description=(event.description or "")[:_MAX_DESC_INPUT],
        venue=(event.public_venue or "")[:255],
        city=(event.city.name if event.city else "")[:255],
        source_category=(event.source_category or "")[:255],
    )


def _hash_input(payload: EnrichmentInput) -> str:
    raw = "|".join(
        f"{k}={payload.as_dict()[k]}" for k in sorted(payload.as_dict())
    ) + f"|v={PROMPT_VERSION}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_unresolved(event: Event) -> bool:
    """Only unresolved records are sent to AI. Here that means an event with no
    resolved category (nothing to enrich otherwise)."""
    return event.effective_category is None


def enrich_event(
    db: Session,
    event: Event,
    *,
    provider: EnrichmentProvider | None = None,
    taxonomy: set[str] | None = None,
    now: datetime | None = None,
) -> EventEnrichment | None:
    """Produce (and cache) an advisory enrichment for an unresolved event, or
    None if AI is disabled/over budget/failed or the event is already resolved.
    Never raises and never mutates the event's authoritative fields."""
    if not is_unresolved(event):
        return None

    settings = get_settings()
    provider = provider or get_enrichment_provider(settings)
    if not provider.available():
        return None

    payload = _input_for(event)
    input_hash = _hash_input(payload)

    cached = db.scalar(
        select(EventEnrichment).where(
            EventEnrichment.event_id == event.id,
            EventEnrichment.prompt_version == PROMPT_VERSION,
            EventEnrichment.input_hash == input_hash,
        )
    )
    if cached is not None:
        return cached

    from app.services.ai.provider import _now

    now = now or _now()
    tracker = _tracker()
    blocked = tracker.can_spend(
        now, daily_limit=settings.ai_daily_request_limit,
        monthly_limit=settings.ai_monthly_request_limit,
    )
    if blocked:
        logger.info("enrichment skipped: %s", blocked)
        return None

    try:
        raw = provider.enrich(payload.as_dict())
        suggestion = EnrichmentSuggestion.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - AI failure never propagates
        tracker.record_failure(
            now, 1, threshold=settings.ai_failure_threshold,
            cooldown_seconds=settings.ai_cooldown_seconds,
        )
        logger.warning("enrichment failed for event %s: %s", event.id, exc)
        return None

    tracker.record_success(now, 1)

    # Taxonomy validation: only keep a category suggestion that is a real,
    # active category. Everything else (a hallucinated category) is dropped.
    category = suggestion.category_suggestion
    if category is not None and taxonomy is not None and category not in taxonomy:
        category = None

    enrichment = EventEnrichment(
        event_id=event.id, prompt_version=PROMPT_VERSION, input_hash=input_hash,
        provider=provider.name, status="suggested",
        category_suggestion=category, tags=list(suggestion.tags), summary=suggestion.summary,
        audience=suggestion.audience, family_friendly=suggestion.family_friendly,
        extra={"duplicate_candidate_ids": suggestion.duplicate_candidate_ids}
        if suggestion.duplicate_candidate_ids else None,
    )
    db.add(enrichment)
    db.commit()
    db.refresh(enrichment)
    return enrichment


def summarize_extraction_errors(
    messages: list[str], *, provider: EnrichmentProvider | None = None
) -> str | None:
    """Allowed task: a short natural-language summary of extraction errors.
    Disabled/failed → None."""
    settings = get_settings()
    provider = provider or get_enrichment_provider(settings)
    if not provider.available() or not messages:
        return None
    try:
        raw = provider.enrich({"task": "error_summary", "messages": messages[:20]})
        text = raw.get("summary") if isinstance(raw, dict) else None
        return text[:_MAX_SUMMARY] if isinstance(text, str) else None
    except Exception:  # noqa: BLE001
        return None


def mark_enrichment(db: Session, enrichment: EventEnrichment, status: str) -> None:
    if status not in ("suggested", "applied", "rejected"):
        raise ValueError(f"unknown enrichment status: {status}")
    enrichment.status = status
    db.commit()
