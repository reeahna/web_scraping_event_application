"""Frozen data types for the AI configuration assistant."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AIConfigurationEvidence:
    """The bounded, sanitized evidence sent to a provider.

    Deliberately small and closed. It carries structural clues about the page,
    never the raw document, and never anything that could identify a person or
    smuggle a credential — see app.services.ai.evidence, which builds it, for
    the exclusion rules.
    """

    listing_url: str
    detected_pattern: str | None
    detector_evidence: dict[str, Any]
    sample_cards_html: tuple[str, ...]
    candidate_selectors: dict[str, list[str]]
    attempted_date_formats: tuple[str, ...]
    validation_failures: tuple[str, ...]
    pagination_indicators: dict[str, Any]
    allowed_pattern_names: tuple[str, ...]

    def as_prompt_dict(self) -> dict[str, Any]:
        return {
            "listing_url": self.listing_url,
            "detected_pattern": self.detected_pattern,
            "detector_evidence": self.detector_evidence,
            "sample_cards_html": list(self.sample_cards_html),
            "candidate_selectors": self.candidate_selectors,
            "attempted_date_formats": list(self.attempted_date_formats),
            "validation_failures": list(self.validation_failures),
            "pagination_indicators": self.pagination_indicators,
            "allowed_pattern_names": list(self.allowed_pattern_names),
        }


@dataclass(frozen=True)
class AISuggestionRequest:
    website_id: int
    evidence: AIConfigurationEvidence
    correlation_id: str | None = None


@dataclass(frozen=True)
class AISuggestionResult:
    """The provider's raw structured answer, before application-side
    validation. `suggestion` is untrusted JSON — it is schema-validated and
    safety-checked by app.services.ai.suggestion before it becomes a draft."""

    ok: bool
    suggestion: dict[str, Any] | None = None
    provider: str = "disabled"
    model: str | None = None
    error: str | None = None
    # Number of provider requests this result consumed (0 for the disabled
    # provider), used to advance the usage counters.
    requests_used: int = 0


@dataclass(frozen=True)
class AIUsageStatus:
    enabled: bool
    provider: str
    healthy: bool
    daily_used: int
    daily_limit: int
    monthly_used: int
    monthly_limit: int
    consecutive_failures: int
    cooldown_active: bool
    notes: tuple[str, ...] = field(default_factory=tuple)
