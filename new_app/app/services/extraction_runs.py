"""ExtractionRunService / ExtractionPreviewService / EventPersistenceService.

Three plain functions — `run_detection`, `preview_extraction`,
`run_extraction` — not three classes, matching this codebase's existing
service-module style (see app.services.categorization, app.services.
websites). This is the only module that opens a Session; every fetch/
detect/extract/normalize/validate/dedup step it calls into (app.extraction.*)
is pure, Session-free, and independently unit-testable against fixtures.

Separation between detection, preview, and persistence is structural, not
just conventional:
  - run_detection never imports or calls anything that writes an Event row.
  - preview_extraction's body never calls repositories.event at all, so
    there is no code path by which a preview could persist anything.
  - run_extraction hard-guards on `website.approved_pattern` being set
    before doing anything else, and reads configuration exclusively from
    that frozen snapshot — never the live `configuration` draft.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.onboarding import ACTIVE, DETECTED, DETECTING, DRAFT, FAILING, UNSUPPORTED
from app.core.onboarding import can_transition as _can_transition
from app.extraction.dedup import dedupe_within_run
from app.extraction.detail_pages import enrich_with_detail_pages
from app.extraction.detection import run_detection as detect_patterns
from app.extraction.fetch import FetchStrategy, HttpFetchStrategy, content_type_allowed
from app.extraction.normalize import normalize_candidate
from app.extraction.pagination import build_pagination_strategy
from app.extraction.registry import REGISTRY
from app.extraction.types import (
    EventCandidate,
    ExtractionResult,
    FetchRequest,
    FetchResponse,
    PatternDetectionResult,
    ValidationResult,
)
from app.extraction.unsupported import build_report
from app.extraction.validate import validate_candidate
from app.models.website import Website
from app.repositories.event import (
    create_event_from_candidate,
    find_existing_event_for_candidate,
    update_event,
)
from app.repositories.event_provenance import create_event_provenance
from app.repositories.extraction_run import create_extraction_run
from app.repositories.unsupported_site_report import (
    create_unsupported_site_report,
    should_create_new_report,
)
from app.schemas.extraction import FetchConfig, SiteConfiguration
from app.services.websites import transition_website

CONSECUTIVE_FAILURE_THRESHOLD = 3


def _draft_fetch_config(website: Website) -> FetchConfig:
    if website.configuration:
        try:
            return SiteConfiguration.model_validate(website.configuration).fetch
        except Exception:
            pass
    return FetchConfig()


def _fallback_timezone(website: Website) -> str | None:
    if website.timezone_override:
        return website.timezone_override
    if website.city is not None:
        return website.city.timezone
    return None


def _response_metadata(response: FetchResponse) -> dict:
    return {
        "status_code": response.status_code,
        "content_type": response.content_type,
        "redirect_count": len(response.redirect_history),
        "body_hash": response.body_hash,
        "truncated": response.truncated,
    }


# --- Detection ------------------------------------------------------------


async def run_detection(
    db: Session, website: Website, *, correlation_id: str | None = None
) -> ExtractionResult:
    listing_url = website.event_listing_url or website.base_url
    if not listing_url:
        raise AppError("Website has no listing URL or base URL to detect against.", status_code=409)

    started_at = datetime.now(UTC)
    fetch = HttpFetchStrategy()
    response = await fetch.fetch(FetchRequest(url=listing_url), _draft_fetch_config(website))
    detection = detect_patterns(response)
    completed_at = datetime.now(UTC)

    if response.blocked_reason is not None:
        status = "blocked"
    elif detection.pattern_name is None:
        status = "unsupported"
    else:
        status = "success"

    # DRAFT can only ever move to DETECTING first — both DETECTED and
    # UNSUPPORTED are reached from there, never directly from DRAFT.
    if website.onboarding_status == DRAFT:
        transition_website(db, website, DETECTING)

    if status == "success":
        proposed_configuration = _build_proposed_configuration(website, detection, listing_url)
        website.proposed_pattern = {
            "detection": _detection_dict(detection, completed_at),
            "configuration": proposed_configuration.model_dump(mode="json"),
        }
        if _can_transition(website.onboarding_status, DETECTED):
            transition_website(db, website, DETECTED)
    else:
        website.proposed_pattern = {
            "detection": _detection_dict(detection, completed_at),
            "configuration": None,
        }
        if _can_transition(website.onboarding_status, UNSUPPORTED):
            transition_website(db, website, UNSUPPORTED)
        report_data = build_report(
            website_id=website.id,
            submitted_url=listing_url,
            response=response,
            detection=detection,
            failure_reason=response.blocked_reason or "no_pattern_matched",
        )
        if should_create_new_report(db, website.id, report_data.fingerprint):
            create_unsupported_site_report(db, report_data)

    db.commit()

    run = create_extraction_run(
        db,
        website_id=website.id,
        configuration_version=website.configuration_version,
        pattern_name=detection.pattern_name,
        run_type="detection",
        status=status,
        source_url=listing_url,
        final_url=response.final_url,
        warnings=list(detection.warnings),
        detector_evidence=detection.evidence,
        response_metadata=_response_metadata(response),
        started_at=started_at,
        completed_at=completed_at,
        correlation_id=correlation_id,
    )

    return ExtractionResult(
        status=status,
        run_id=run.id,
        pattern=detection.pattern_name,
        source_url=listing_url,
        final_url=response.final_url,
        events_found=0,
        events_valid=0,
        events_rejected=0,
        events_inserted=0,
        events_updated=0,
        duplicates_skipped=0,
        warnings=tuple(detection.warnings),
        errors=(),
        evidence=detection.evidence,
    )


def _detection_dict(detection: PatternDetectionResult, detected_at: datetime) -> dict:
    return {
        "pattern_name": detection.pattern_name,
        "confidence": detection.confidence,
        "evidence": detection.evidence,
        "discovered_endpoints": list(detection.discovered_endpoints),
        "browser_required": detection.browser_required,
        "warnings": list(detection.warnings),
        "detector_version": detection.detector_version,
        "detected_at": detected_at.isoformat(),
    }


def _build_proposed_configuration(
    website: Website, detection: PatternDetectionResult, listing_url: str
) -> SiteConfiguration:
    kwargs: dict = {"pattern_name": detection.pattern_name}
    if detection.pattern_name == "wordpress_rest" and detection.discovered_endpoints:
        kwargs["api_endpoint"] = detection.discovered_endpoints[0]
    else:
        kwargs["listing_url"] = listing_url
    return SiteConfiguration(**kwargs)


# --- Shared pipeline (preview + persistent runs) --------------------------


@dataclass(frozen=True)
class _PipelineOutcome:
    outcomes: list[tuple[EventCandidate, ValidationResult]]
    warnings: list[str]
    first_response: FetchResponse | None
    last_response: FetchResponse | None
    response_hash_by_page: dict[str, str]


async def _execute_pipeline(
    config: SiteConfiguration,
    pattern_name: str,
    fetch: FetchStrategy,
    *,
    fallback_timezone: str | None,
) -> _PipelineOutcome:
    registration = REGISTRY.get(pattern_name)
    pattern = registration.extractor
    pagination = build_pagination_strategy(config)

    request_url = config.api_endpoint or config.listing_url
    if not request_url:
        raise AppError("Site configuration has no listing_url or api_endpoint", status_code=409)

    warnings: list[str] = []
    all_candidates: list[EventCandidate] = []
    response_hash_by_page: dict[str, str] = {}
    visited_urls: set[str] = set()
    seen_hashes: set[str] = set()

    current_request: FetchRequest | None = FetchRequest(
        url=request_url,
        method=config.fetch.method,
        headers=config.fetch.headers,
        params=config.fetch.query_params,
        json_body=config.fetch.json_body,
    )
    response: FetchResponse | None = None
    first_response: FetchResponse | None = None
    page_index = 0
    max_events_reached = False

    while current_request is not None:
        response = await fetch.fetch(current_request, config.fetch)
        if first_response is None:
            first_response = response
        if response.blocked_reason is not None:
            break
        if not content_type_allowed(response.content_type, config.fetch):
            warnings.append(f"unexpected_content_type:{response.content_type}")
            break

        visited_urls.add(response.final_url)
        seen_hashes.add(response.body_hash)
        response_hash_by_page[response.final_url] = response.body_hash

        page_candidates = pattern.extract(response, config)
        remaining_capacity = config.pagination.max_events - len(all_candidates)
        if len(page_candidates) > remaining_capacity:
            page_candidates = page_candidates[: max(remaining_capacity, 0)]
            max_events_reached = True

        all_candidates.extend(page_candidates)
        if max_events_reached:
            warnings.append("max_events_reached")
            break

        next_request = pagination.next_request(
            response,
            page_index,
            config,
            visited_urls=frozenset(visited_urls),
            seen_body_hashes=frozenset(seen_hashes),
        )
        if next_request is None:
            break
        current_request = next_request
        page_index += 1

    if response is not None and response.blocked_reason is None:
        wants_detail_fetch = bool(config.detail_page_selector) or any(
            key.startswith("detail_") for key in config.field_selectors
        )
        if wants_detail_fetch:
            all_candidates = await enrich_with_detail_pages(all_candidates, fetch, config)

    normalized = [
        normalize_candidate(c, config, fallback_timezone=config.timezone or fallback_timezone)
        for c in all_candidates
    ]
    outcomes = [(c, validate_candidate(c, config)) for c in normalized]

    return _PipelineOutcome(
        outcomes=outcomes,
        warnings=warnings,
        first_response=first_response,
        last_response=response,
        response_hash_by_page=response_hash_by_page,
    )


def _compute_status(*, blocked: bool, events_found: int, events_valid: int) -> str:
    if blocked:
        return "blocked"
    if events_found == 0 or events_valid == 0:
        return "failed"
    if events_valid < events_found:
        return "partial"
    return "success"


def _resolve_configuration(website: Website, *, use_approved: bool) -> SiteConfiguration:
    source = website.approved_pattern if use_approved else (website.configuration or {})
    if not source:
        stage = "approved_pattern" if use_approved else "configuration"
        raise AppError(f"Website has no {stage} to extract from.", status_code=409)
    return SiteConfiguration.model_validate(source)


# --- Preview ---------------------------------------------------------------


async def preview_extraction(
    db: Session, website: Website, *, correlation_id: str | None = None
) -> ExtractionResult:
    """Builds from the *draft* configuration (an admin previews before
    approving). Never calls app.repositories.event — there is no code path
    here that can persist an Event row."""
    config = _resolve_configuration(website, use_approved=False)
    started_at = datetime.now(UTC)
    fetch = HttpFetchStrategy()
    outcome = await _execute_pipeline(
        config, config.pattern_name, fetch, fallback_timezone=_fallback_timezone(website)
    )
    completed_at = datetime.now(UTC)

    valid = [c for c, result in outcome.outcomes if result.is_valid]
    rejected = [(c, result) for c, result in outcome.outcomes if not result.is_valid]
    blocked = outcome.last_response is not None and outcome.last_response.blocked_reason is not None
    status = _compute_status(
        blocked=blocked, events_found=len(outcome.outcomes), events_valid=len(valid)
    )

    warnings = list(outcome.warnings)
    errors = [
        f"candidate[{i}]: {err}" for i, (_, result) in enumerate(rejected) for err in result.errors
    ]
    if blocked and outcome.last_response is not None:
        warnings.append(str(outcome.last_response.blocked_reason))

    run = create_extraction_run(
        db,
        website_id=website.id,
        configuration_version=website.configuration_version,
        pattern_name=config.pattern_name,
        run_type="preview",
        status=status,
        source_url=config.api_endpoint or config.listing_url or "",
        final_url=outcome.last_response.final_url if outcome.last_response else None,
        events_found=len(outcome.outcomes),
        events_valid=len(valid),
        events_rejected=len(rejected),
        warnings=warnings,
        error_summary="; ".join(errors[:5]) or None,
        response_metadata=_response_metadata(outcome.last_response)
        if outcome.last_response
        else None,
        started_at=started_at,
        completed_at=completed_at,
        correlation_id=correlation_id,
    )

    return ExtractionResult(
        status=status,
        run_id=run.id,
        pattern=config.pattern_name,
        source_url=config.api_endpoint or config.listing_url or "",
        final_url=outcome.last_response.final_url if outcome.last_response else None,
        events_found=len(outcome.outcomes),
        events_valid=len(valid),
        events_rejected=len(rejected),
        events_inserted=0,
        events_updated=0,
        duplicates_skipped=0,
        warnings=tuple(warnings),
        errors=tuple(errors),
        evidence={},
    )


# --- Persistent (manual) extraction ----------------------------------------


async def run_extraction(
    db: Session,
    website: Website,
    *,
    triggered_by_user_id: int | None,
    correlation_id: str | None = None,
) -> ExtractionResult:
    """Requires an approved configuration. Reads exclusively from
    `website.approved_pattern` — the frozen snapshot — never the live draft."""
    if not website.approved_pattern:
        raise AppError(
            "Website has no approved configuration. Approve a configuration before running "
            "extraction.",
            status_code=409,
        )

    config = _resolve_configuration(website, use_approved=True)
    started_at = datetime.now(UTC)
    fetch = HttpFetchStrategy()
    outcome = await _execute_pipeline(
        config, config.pattern_name, fetch, fallback_timezone=_fallback_timezone(website)
    )
    completed_at = datetime.now(UTC)

    valid = [c for c, result in outcome.outcomes if result.is_valid]
    rejected = [(c, result) for c, result in outcome.outcomes if not result.is_valid]
    blocked = outcome.last_response is not None and outcome.last_response.blocked_reason is not None
    status = _compute_status(
        blocked=blocked, events_found=len(outcome.outcomes), events_valid=len(valid)
    )

    warnings = list(outcome.warnings)
    errors = [
        f"candidate[{i}]: {err}" for i, (_, result) in enumerate(rejected) for err in result.errors
    ]
    if blocked and outcome.last_response is not None:
        warnings.append(str(outcome.last_response.blocked_reason))

    # Created now (counts filled in below) so every persisted EventProvenance
    # row can carry a real extraction_run_id from the start.
    run = create_extraction_run(
        db,
        website_id=website.id,
        configuration_version=website.active_configuration_version,
        pattern_name=config.pattern_name,
        run_type="manual",
        status=status,
        source_url=config.api_endpoint or config.listing_url or "",
        final_url=outcome.last_response.final_url if outcome.last_response else None,
        events_found=len(outcome.outcomes),
        events_valid=len(valid),
        events_rejected=len(rejected),
        warnings=warnings,
        error_summary="; ".join(errors[:5]) or None,
        response_metadata=_response_metadata(outcome.last_response)
        if outcome.last_response
        else None,
        started_at=started_at,
        completed_at=completed_at,
        initiating_user_id=triggered_by_user_id,
        correlation_id=correlation_id,
    )

    dedup_outcome = dedupe_within_run(valid, website_id=website.id, city_id=website.city_id)

    events_inserted = 0
    events_updated = 0
    for candidate in dedup_outcome.kept:
        existing = find_existing_event_for_candidate(
            db, candidate, website_id=website.id, city_id=website.city_id
        )
        if existing is not None:
            event = update_event(db, existing, candidate)
            events_updated += 1
        else:
            event = create_event_from_candidate(
                db,
                candidate,
                website_id=website.id,
                city_id=website.city_id,
                source=website.source_display_name or website.name,
            )
            events_inserted += 1

        create_event_provenance(
            db,
            event_id=event.id,
            extraction_run_id=run.id,
            website_id=website.id,
            source_page=candidate.source_page,
            extraction_pattern=candidate.extraction_pattern,
            pattern_version=REGISTRY.get(config.pattern_name).version,
            raw_record_hash=candidate.raw_record_hash,
            source_response_hash=outcome.response_hash_by_page.get(candidate.source_page, ""),
            field_source_paths=candidate.field_source_paths,
            transformation_history=list(candidate.transformation_history),
        )

    run.events_inserted = events_inserted
    run.events_updated = events_updated
    run.duplicates_skipped = dedup_outcome.duplicates_skipped
    db.commit()

    _update_website_health(db, website, status)

    return ExtractionResult(
        status=status,
        run_id=run.id,
        pattern=config.pattern_name,
        source_url=config.api_endpoint or config.listing_url or "",
        final_url=outcome.last_response.final_url if outcome.last_response else None,
        events_found=len(outcome.outcomes),
        events_valid=len(valid),
        events_rejected=len(rejected),
        events_inserted=events_inserted,
        events_updated=events_updated,
        duplicates_skipped=dedup_outcome.duplicates_skipped,
        warnings=tuple(warnings),
        errors=tuple(errors),
        evidence={},
    )


def _update_website_health(db: Session, website: Website, status: str) -> None:
    now = datetime.now(UTC)
    if status in ("success", "partial"):
        website.last_success_at = now
        website.consecutive_failure_count = 0
    elif status in ("failed", "blocked"):
        website.last_failure_at = now
        website.consecutive_failure_count += 1
        if (
            website.consecutive_failure_count >= CONSECUTIVE_FAILURE_THRESHOLD
            and website.onboarding_status == ACTIVE
        ):
            transition_website(db, website, FAILING)
    db.commit()
    db.refresh(website)
