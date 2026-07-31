"""Restricted-browser recovery for sources ordinary HTTP detection can't read.

The primary onboarding path stays automatic HTTP detection. This is the
*fallback* the administrator triggers when that returns unsupported or
needs-review: it renders the listing once in the locked-down Playwright
strategy (app.extraction.browser, via app.services.browser_observation),
re-runs the ordinary PatternRegistry detection over the rendered HTML and any
observed JSON/feed responses, and — preferring a reusable structured HTTP
endpoint when one is found — drives the *same* propose → draft → preview →
policy pipeline the HTTP path uses. Nothing here is site-specific.

Guarantees carried over from that pipeline: no Event row is ever persisted
(preview has no path to app.repositories.event); only a draft configuration is
written; `configuration_version` bumps so stale-preview protection holds; and
the Phase 8D policy still decides approval/activation (off by default). The
only thing this module adds is the browser step and a bounded, redacted record
of it on the source's unsupported-site report.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.auto_onboarding import (
    ORIGIN_DETERMINISTIC_GENERIC_HTML,
    ORIGIN_DETERMINISTIC_STRUCTURED,
)
from app.core.exceptions import AppError
from app.core.onboarding import NEEDS_REVIEW as ONBOARDING_NEEDS_REVIEW
from app.core.onboarding import can_transition
from app.extraction.inference.policy import (
    BLOCKED,
    DEFAULT_POLICY,
    FAILED,
    NEEDS_REVIEW,
    READY_FOR_APPROVAL,
    AutoOnboardingPolicy,
)
from app.extraction.inference.quality import meets_approval_bar
from app.extraction.inference.service import ConfigurationInferenceService
from app.extraction.inference.types import InferenceResult
from app.extraction.registry import REGISTRY
from app.extraction.types import ExtractionResult
from app.models.website import Website
from app.repositories.unsupported_site_report import (
    get_latest_report_for_website,
    set_browser_recovery,
)
from app.schemas.browser import BrowserPlan
from app.services import extraction_runs
from app.services.browser_observation import (
    OUTCOME_BLOCKED,
    OUTCOME_STRUCTURED_PATTERN_NEEDED,
    BrowserObservation,
    render_and_observe,
)
from app.services.onboarding_automation import (
    _evaluate_and_execute_policy,
    _fallback_timezone,
    _finish,
    _resolve_detail_probe,
)
from app.services.website_configuration import save_draft_configuration
from app.services.websites import transition_website

# Recovery outcome constants. `RECOVERY_BLOCKED` / `RECOVERY_UNSUPPORTED` mean
# no configuration was produced; the rest mirror the inference outcomes.
RECOVERY_BLOCKED = "blocked"
RECOVERY_UNSUPPORTED = "unsupported"
RECOVERY_NEEDS_REVIEW = "needs_review"
# A qualifying first-party event endpoint was found but no registered pattern
# can extract it. Distinct from the outcomes above: no draft/preview is run.
RECOVERY_STRUCTURED_PATTERN_NEEDED = "structured_pattern_needed"


@dataclass(frozen=True)
class BrowserRecoveryResult:
    status: str
    observation: BrowserObservation | None
    onboarding: object | None = None  # AutoOnboardingResult when a config was produced


def _strip_query(url: str) -> str:
    """Endpoint URL without query string or fragment — a discovered endpoint is
    worth recording, but its query may carry keys/tokens we must never store."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _evidence_base() -> dict:
    return {
        "attempted_at": datetime.now(UTC).isoformat(),
        "status": None,
        "rendered_status": None,
        "observed_response_types": [],
        "discovered_endpoints": [],
        "candidate_event_endpoints": [],
        "ignored_endpoint_count": 0,
        "ignored_endpoints": [],
        "selected_endpoint": None,
        "selection_reason": None,
        "rejected_candidates": [],
        "response_shape": None,
        "new_pattern_needed": False,
        "candidate_fingerprint": None,
        "attempts": 1,
        "last_attempt_at": None,
        "chosen_source": None,
        "detected_pattern": None,
        "detection_confidence": None,
        "proposed_pattern": None,
        "preview_status": None,
        "blocked_reason": None,
        "error_summary": None,
    }


def _observation_evidence(evidence: dict, observation: BrowserObservation) -> None:
    """Fills the redacted evidence dict from an observation. Only bounded,
    non-secret fields — never cookies, headers, credentials, tokens, bodies, or
    raw query values (every URL is stored query-stripped)."""
    response_types: list[str] = []
    if observation.rendered_response is not None:
        response_types.append("rendered_html")
        evidence["rendered_status"] = observation.rendered_response.status_code
    if observation.api_responses:
        response_types.append("json")
    evidence["observed_response_types"] = response_types
    evidence["discovered_endpoints"] = [
        _strip_query(r.final_url) for r in observation.api_responses
    ]
    # Candidate event endpoints and ignored third-party endpoints kept
    # distinct, so telemetry is never presented as a viable event source.
    evidence["candidate_event_endpoints"] = [
        c.to_evidence() for c in observation.candidate_endpoints
    ]
    evidence["ignored_endpoints"] = [
        {"url": c.sanitized_url, "origin": c.origin, "classification": c.classification}
        for c in observation.ignored_endpoints
    ]
    evidence["ignored_endpoint_count"] = len(observation.ignored_endpoints)
    evidence["selected_endpoint"] = observation.selected_endpoint
    evidence["selection_reason"] = observation.selection_reason
    evidence["rejected_candidates"] = observation.rejected_candidates
    evidence["new_pattern_needed"] = observation.new_pattern_needed
    top = observation.candidate_endpoints[0] if observation.candidate_endpoints else None
    if top is not None:
        evidence["response_shape"] = {
            "top_level_type": top.top_level_type,
            "top_level_keys": top.top_level_keys,
            "record_array_path": top.record_array_path,
            "sample_field_names": top.sample_field_names,
            "sample_record_count": top.sample_record_count,
            "event_likeness_score": round(top.event_likeness_score, 4),
        }
    evidence["chosen_source"] = observation.chosen_source
    if observation.detection is not None:
        evidence["detected_pattern"] = observation.detection.pattern_name
        evidence["detection_confidence"] = observation.detection.confidence
    if observation.warnings:
        evidence["error_summary"] = ", ".join(observation.warnings[:10])


def _persist(db: Session, website: Website, evidence: dict) -> None:
    report = get_latest_report_for_website(db, website.id)
    if report is not None:
        set_browser_recovery(db, report, evidence=evidence)


def _candidate_fingerprint(analysis) -> str:
    """A bounded, deterministic identity for an unextractable candidate, so
    equivalent retries are recognised as the same and don't re-record. Uses the
    sanitized endpoint, response-shape signature, record-array path, a
    sample-field-name signature, and a coarse event-likeness bucket."""
    score_bucket = int(round(analysis.event_likeness_score * 10))
    field_sig = ",".join(sorted(analysis.sample_field_names))
    raw = "|".join(
        [
            analysis.sanitized_url,
            analysis.top_level_type,
            analysis.record_array_path or "",
            field_sig,
            str(score_bucket),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _handle_structured_pattern_needed(
    db: Session, website: Website, observation: BrowserObservation, evidence: dict
) -> BrowserRecoveryResult:
    """A first-party event endpoint is preferred but no registered pattern can
    extract it. Never proposes, drafts, previews, or bumps configuration_version
    — it records the candidate analysis, keeps the source in review, and is
    idempotent across equivalent retries."""
    top = observation.candidate_endpoints[0]
    fingerprint = _candidate_fingerprint(top)
    evidence["status"] = RECOVERY_STRUCTURED_PATTERN_NEEDED
    evidence["candidate_fingerprint"] = fingerprint
    evidence["last_attempt_at"] = evidence["attempted_at"]

    report = get_latest_report_for_website(db, website.id)
    if report is not None:
        # A fresh dict, not the stored object — reassigning the same instance
        # would not mark the JSON column dirty and the update would be lost.
        prior = dict(report.browser_recovery or {})
        if prior.get("candidate_fingerprint") == fingerprint:
            # Equivalent retry: bump attempt tracking only, leave the rest of
            # the (identical) evidence untouched.
            prior["attempts"] = int(prior.get("attempts", 1)) + 1
            prior["last_attempt_at"] = evidence["attempted_at"]
            set_browser_recovery(db, report, evidence=prior)
        else:
            evidence["attempts"] = 1
            set_browser_recovery(db, report, evidence=evidence)

    # Keep (or move to) review — a plausible source exists, it just needs a
    # reusable pattern. configuration_version is deliberately never touched.
    if can_transition(website.onboarding_status, ONBOARDING_NEEDS_REVIEW):
        transition_website(db, website, ONBOARDING_NEEDS_REVIEW)
    return BrowserRecoveryResult(
        status=RECOVERY_STRUCTURED_PATTERN_NEEDED, observation=observation
    )


async def browser_retry_recovery(
    db: Session,
    website: Website,
    *,
    correlation_id: str | None = None,
    policy: AutoOnboardingPolicy = DEFAULT_POLICY,
    strategy=None,
    plan: BrowserPlan | None = None,
    submitted_by_user_id: int | None = None,
    submitter_role_ids: frozenset[int] = frozenset(),
) -> BrowserRecoveryResult:
    settings = get_settings()
    if not settings.browser_extraction_enabled:
        raise AppError("Restricted browser detection is disabled for this deployment.", 409)
    if website.archived_at is not None:
        raise AppError("An archived website cannot be retried.", 409)
    listing_url = website.event_listing_url or website.base_url
    if not listing_url:
        raise AppError("Website has no listing URL or base URL to detect against.", 409)

    observation = await render_and_observe(listing_url, plan=plan, strategy=strategy)

    evidence = _evidence_base()

    # Branch on the single, authoritative observation outcome. Nothing infers
    # the source from a combination of chosen_source/detection/new_pattern_needed
    # that could contradict itself.

    # --- Blocked: challenge / CAPTCHA / login wall / SSRF / timeout --------
    if observation.outcome == OUTCOME_BLOCKED:
        evidence["status"] = RECOVERY_BLOCKED
        evidence["blocked_reason"] = observation.blocked_reason
        if observation.warnings:
            evidence["error_summary"] = ", ".join(observation.warnings[:10])
        _persist(db, website, evidence)
        return BrowserRecoveryResult(status=RECOVERY_BLOCKED, observation=observation)

    _observation_evidence(evidence, observation)

    # --- Structured event endpoint found, but no pattern can extract it ----
    # Do NOT propose, draft, preview, or bump configuration_version. Idempotent.
    if observation.outcome == OUTCOME_STRUCTURED_PATTERN_NEEDED:
        return _handle_structured_pattern_needed(db, website, observation, evidence)

    # --- No source at all -------------------------------------------------
    if observation.detection is None or observation.detection.pattern_name is None:
        evidence["status"] = RECOVERY_UNSUPPORTED
        _persist(db, website, evidence)
        return BrowserRecoveryResult(status=RECOVERY_UNSUPPORTED, observation=observation)

    # --- Selected source (structured_selected / rendered_selected): run the
    #     ordinary propose -> draft -> preview -> policy pipeline ------------
    # A source that browser detection can now read belongs in review, not left
    # UNSUPPORTED. From UNSUPPORTED the only forward state is NEEDS_REVIEW, so
    # approval stays a separate, permissioned step — recovery never activates.
    # A source that browser detection can now read belongs in review, not left
    # UNSUPPORTED. From UNSUPPORTED the only forward state is NEEDS_REVIEW, so
    # approval stays a separate, permissioned step — recovery never activates.
    if can_transition(website.onboarding_status, ONBOARDING_NEEDS_REVIEW):
        transition_website(db, website, ONBOARDING_NEEDS_REVIEW)

    detection = observation.detection
    chosen = observation.chosen_response
    service = ConfigurationInferenceService(REGISTRY, policy=policy)
    context = service.build_context(
        response=chosen,
        detection=detection,
        listing_url=chosen.final_url,
        fallback_timezone=_fallback_timezone(website),
    )
    proposal = service.propose(context)
    detection_result = _synthetic_detection_result(observation, listing_url)

    if isinstance(proposal, InferenceResult):
        evidence["status"] = proposal.outcome
        _persist(db, website, evidence)
        result = _finish(db, website, detection_result, proposal, None, None, ())
        return BrowserRecoveryResult(
            status=proposal.outcome, observation=observation, onboarding=result
        )

    proposal = await _resolve_detail_probe(service, context, proposal)
    inference = service.finalize(proposal, detection)
    if inference.configuration is None:
        evidence["status"] = inference.outcome
        _persist(db, website, evidence)
        result = _finish(db, website, detection_result, inference, None, None, ())
        return BrowserRecoveryResult(
            status=inference.outcome, observation=observation, onboarding=result
        )

    save_draft_configuration(db, website, inference.configuration)
    website.configuration_origin = (
        ORIGIN_DETERMINISTIC_GENERIC_HTML
        if inference.configuration.pattern_name == "generic_html_cards"
        else ORIGIN_DETERMINISTIC_STRUCTURED
    )
    db.commit()

    preview = await extraction_runs.preview_extraction_detailed(
        db, website, correlation_id=correlation_id
    )
    ok, reasons = meets_approval_bar(preview.quality, policy, preview_status=preview.result.status)
    # This path is only reached for a genuinely selected source (structured_api
    # or rendered HTML with no competing structured candidate), so a failed
    # preview here is a real failure — there is no unextractable candidate to
    # defer to (that case never reaches this branch).
    if preview.result.status == "blocked":
        outcome = BLOCKED
    elif inference.missing_required_fields:
        outcome = NEEDS_REVIEW
    elif preview.result.status == "failed":
        outcome = FAILED
    elif inference.outcome == READY_FOR_APPROVAL and ok:
        outcome = READY_FOR_APPROVAL
    else:
        outcome = NEEDS_REVIEW

    blocking = tuple(
        [*(f"missing required field: {f}" for f in inference.missing_required_fields), *reasons]
    )

    decision, execution = await _evaluate_and_execute_policy(
        db,
        website,
        detection_status="success",
        browser_required=False,
        inference=inference,
        preview=preview,
        submitted_by_user_id=submitted_by_user_id,
        onboarding_job_id=None,
        onboarding_batch_id=None,
        submitter_role_ids=submitter_role_ids,
        correlation_id=correlation_id,
    )
    from app.core.auto_onboarding import AUTOMATICALLY_ACTIVATED, AUTOMATICALLY_APPROVED

    if execution is not None:
        if execution.activated:
            outcome = AUTOMATICALLY_ACTIVATED
        elif execution.approved:
            outcome = AUTOMATICALLY_APPROVED

    evidence["status"] = outcome
    evidence["proposed_pattern"] = inference.configuration.pattern_name
    evidence["preview_status"] = preview.result.status
    _persist(db, website, evidence)

    result = _finish(
        db,
        website,
        detection_result,
        inference,
        preview.result,
        preview.quality,
        blocking,
        valid_samples=preview.valid_samples,
        rejected_samples=preview.rejected_samples,
        outcome_override=outcome,
        decision=decision,
        execution=execution,
    )
    return BrowserRecoveryResult(status=outcome, observation=observation, onboarding=result)


def _synthetic_detection_result(
    observation: BrowserObservation, listing_url: str
) -> ExtractionResult:
    """A minimal ExtractionResult standing in for the browser-observed detection
    so the shared `_finish` can record the onboarding result the same way the
    HTTP path does. Carries no persisted run of its own."""
    detection = observation.detection
    chosen = observation.chosen_response
    return ExtractionResult(
        status="success" if detection and detection.pattern_name else "unsupported",
        run_id=None,
        pattern=detection.pattern_name if detection else None,
        source_url=listing_url,
        final_url=(chosen.final_url if chosen else listing_url),
        events_found=0,
        events_valid=0,
        events_rejected=0,
        events_inserted=0,
        events_updated=0,
        duplicates_skipped=0,
        warnings=tuple(detection.warnings) if detection else (),
        errors=(),
        evidence=detection.evidence if detection else {},
    )
