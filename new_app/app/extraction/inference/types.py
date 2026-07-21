"""Frozen data types for configuration inference.

Every type here is plain, JSON-serializable-on-demand data (via `as_dict`)
so an inference result can be stored verbatim on `Website.proposed_pattern`
and rendered by the admin UI without any further translation layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclasses_field
from typing import TYPE_CHECKING, Any, Literal

from app.extraction.types import FetchResponse, PatternDetectionResult
from app.schemas.extraction import SiteConfiguration

if TYPE_CHECKING:
    from app.extraction.inference.policy import AutoOnboardingPolicy


@dataclass(frozen=True)
class FieldSelectorCandidate:
    """One proposed (or rejected) way to resolve a single event field.

    Records everything an administrator needs to judge the proposal without
    opening the page's HTML: what was chosen, how confident the scorer was,
    which observations produced that score, what the values actually looked
    like, and what else was considered.
    """

    field: str
    kind: Literal["css", "json_path"]
    selector: str | None
    attribute: str | None
    confidence: float
    coverage: float
    parse_success_rate: float | None
    evidence: tuple[str, ...]
    sample_values: tuple[str, ...]
    warnings: tuple[str, ...]
    alternatives: tuple[str, ...]
    accepted: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "kind": self.kind,
            "selector": self.selector,
            "attribute": self.attribute,
            "confidence": round(self.confidence, 3),
            "coverage": round(self.coverage, 3),
            "parse_success_rate": (
                None if self.parse_success_rate is None else round(self.parse_success_rate, 3)
            ),
            "evidence": list(self.evidence),
            "sample_values": list(self.sample_values),
            "warnings": list(self.warnings),
            "alternatives": list(self.alternatives),
            "accepted": self.accepted,
        }


@dataclass(frozen=True)
class DateFormatCandidate:
    kind: Literal["date", "time"]
    format: str
    match_rate: float
    samples: tuple[str, ...]
    evidence: tuple[str, ...]
    warnings: tuple[str, ...]
    accepted: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "format": self.format,
            "match_rate": round(self.match_rate, 3),
            "samples": list(self.samples),
            "evidence": list(self.evidence),
            "warnings": list(self.warnings),
            "accepted": self.accepted,
        }


@dataclass(frozen=True)
class ProposalContext:
    """Everything a proposer is allowed to look at. Deliberately holds the
    single already-fetched response — a proposer never performs I/O, so
    proposing a configuration can never trigger an extra network request."""

    response: FetchResponse
    detection: PatternDetectionResult
    listing_url: str
    fallback_timezone: str | None
    policy: AutoOnboardingPolicy
    # url -> HTML text. Populated by the caller only after a first proposal
    # round returned `detail_probe_url`; the proposer itself never fetches,
    # so the second round stays a pure function of data it was handed.
    detail_documents: dict[str, str] = dataclasses_field(default_factory=dict)


@dataclass(frozen=True)
class ConfigurationProposal:
    configuration: SiteConfiguration | None
    field_candidates: tuple[FieldSelectorCandidate, ...] = ()
    date_format_candidates: tuple[DateFormatCandidate, ...] = ()
    confidence: float = 0.0
    missing_required_fields: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    error: str | None = None
    # Non-None means "I need exactly one bounded detail-page document before
    # I can finish" — see ProposalContext.detail_documents.
    detail_probe_url: str | None = None


@dataclass(frozen=True)
class InferenceResult:
    outcome: str
    pattern_name: str | None
    detection_confidence: float
    proposal_confidence: float
    configuration: SiteConfiguration | None
    field_candidates: tuple[FieldSelectorCandidate, ...]
    date_format_candidates: tuple[DateFormatCandidate, ...]
    missing_required_fields: tuple[str, ...]
    warnings: tuple[str, ...]
    notes: tuple[str, ...]
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "pattern_name": self.pattern_name,
            "detection_confidence": round(self.detection_confidence, 3),
            "proposal_confidence": round(self.proposal_confidence, 3),
            # Included so the review screen renders the proposal from one
            # stored object rather than having to re-read the draft.
            "configuration": (
                self.configuration.model_dump(mode="json") if self.configuration else None
            ),
            "field_candidates": [c.as_dict() for c in self.field_candidates],
            "date_format_candidates": [c.as_dict() for c in self.date_format_candidates],
            "missing_required_fields": list(self.missing_required_fields),
            "warnings": list(self.warnings),
            "notes": list(self.notes),
            "error": self.error,
        }


@dataclass(frozen=True)
class PreviewQualityResult:
    candidates_found: int
    valid_count: int
    rejected_count: int
    valid_percentage: float
    rejected_percentage: float
    required_field_coverage: dict[str, float]
    date_parse_success_rate: float
    url_validity_rate: float
    duplicate_rate: float
    warning_count: int
    pagination_truncated: bool
    detail_fetch_used: bool
    pages_fetched: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidates_found": self.candidates_found,
            "valid_count": self.valid_count,
            "rejected_count": self.rejected_count,
            "valid_percentage": round(self.valid_percentage, 3),
            "rejected_percentage": round(self.rejected_percentage, 3),
            "required_field_coverage": {
                k: round(v, 3) for k, v in self.required_field_coverage.items()
            },
            "date_parse_success_rate": round(self.date_parse_success_rate, 3),
            "url_validity_rate": round(self.url_validity_rate, 3),
            "duplicate_rate": round(self.duplicate_rate, 3),
            "warning_count": self.warning_count,
            "pagination_truncated": self.pagination_truncated,
            "detail_fetch_used": self.detail_fetch_used,
            "pages_fetched": self.pages_fetched,
        }
