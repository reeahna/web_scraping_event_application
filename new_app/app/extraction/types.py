"""Frozen data types shared across the extraction engine.

Every type here is plain data (no Session, no network I/O) so the engine
stays unit-testable against fixtures. Dataclasses are frozen — each pipeline
stage (fetch -> detect -> extract -> normalize -> validate -> dedup) returns
a new value rather than mutating its input, which is what makes deterministic
repeated-run comparisons meaningful.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from app.schemas.extraction import SiteConfiguration

RunStatus = Literal[
    "success", "partial", "needs_review", "unsupported", "blocked", "failed", "cancelled"
]
RunType = Literal["detection", "preview", "manual", "scheduled"]


@dataclass(frozen=True)
class FetchRequest:
    url: str
    method: Literal["GET", "POST"] = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)
    json_body: dict[str, Any] | None = None


@dataclass(frozen=True)
class FetchResponse:
    request_url: str
    final_url: str
    status_code: int
    headers: dict[str, str]
    content_type: str | None
    body: bytes
    redirect_history: tuple[str, ...]
    body_hash: str
    elapsed_seconds: float
    blocked_reason: str | None = None
    truncated: bool = False

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class PatternDetectionResult:
    pattern_name: str | None
    confidence: float
    evidence: dict[str, Any]
    discovered_endpoints: tuple[str, ...]
    browser_required: bool
    warnings: tuple[str, ...]
    detector_version: str
    needs_review: bool


@dataclass(frozen=True)
class FieldExtractionResult:
    value: Any
    source_path: str | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class EventCandidate:
    """An in-flight extracted event — never an ORM object. Only candidates
    that pass EventValidator ever reach app.repositories.event."""

    raw: dict[str, Any]
    title: str | None
    canonical_url: str | None
    description: str | None
    start_date: date | None
    start_time: time | None
    end_date: date | None
    end_time: time | None
    timezone: str | None
    venue: str | None
    address: str | None
    image_url: str | None
    latitude: float | None
    longitude: float | None
    source_category: str | None
    external_source_id: str | None
    field_source_paths: dict[str, str]
    transformation_history: tuple[str, ...]
    source_page: str
    extraction_pattern: str
    warnings: tuple[str, ...]
    raw_record_hash: str


@dataclass(frozen=True)
class ValidationResult:
    is_valid: bool
    errors: tuple[str, ...]


@dataclass(frozen=True)
class ExtractionResult:
    status: RunStatus
    run_id: int | None
    pattern: str | None
    source_url: str
    final_url: str | None
    events_found: int
    events_valid: int
    events_rejected: int
    events_inserted: int
    events_updated: int
    duplicates_skipped: int
    warnings: tuple[str, ...]
    errors: tuple[str, ...]
    evidence: dict[str, Any]


@dataclass(frozen=True)
class SiteDefinition:
    """Binds one website + a resolved configuration + the resolved pattern
    name into the single object the engine's run functions take. Built by
    app.services.extraction_runs, which decides whether `configuration` came
    from the approved snapshot (persistent runs) or the editable draft
    (detection/preview runs)."""

    website_id: int
    pattern_name: str
    configuration: SiteConfiguration
