"""Automatic website onboarding: URL in, ready-to-approve result out.

One entry point, `detect_and_configure`, runs the whole sequence an
administrator previously had to drive by hand:

    fetch -> detect -> propose configuration -> save draft -> preview
          -> score preview -> classify outcome

Every step is an existing production service. Detection is
`extraction_runs.run_detection_detailed` (so status transitions, run rows,
unsupported-site reports and notifications all behave exactly as before);
the draft is written through `website_configuration.save_draft_configuration`
(so `configuration_version` bumps normally and approval's
"preview must match the current version" rule keeps working); the preview is
`extraction_runs.preview_extraction_detailed`, which has no code path to
app.repositories.event and therefore cannot persist an Event row.

This module chooses *nothing* per site: the proposer comes from the pattern
registry, and every threshold comes from AutoOnboardingPolicy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.auto_onboarding import (
    AUTOMATICALLY_ACTIVATED,
    AUTOMATICALLY_APPROVED,
    ORIGIN_DETERMINISTIC_GENERIC_HTML,
    ORIGIN_DETERMINISTIC_STRUCTURED,
)
from app.core.onboarding import NEEDS_REVIEW as ONBOARDING_NEEDS_REVIEW
from app.core.onboarding import can_transition
from app.extraction.fetch import content_type_allowed
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
from app.extraction.inference.types import (
    ConfigurationProposal,
    InferenceResult,
    PreviewQualityResult,
)
from app.extraction.registry import REGISTRY
from app.extraction.types import EventCandidate, ExtractionResult, FetchRequest
from app.models.extraction_run import ExtractionRun
from app.models.onboarding_batch import OnboardingBatch
from app.models.website import Website
from app.repositories.auto_onboarding import (
    policy_city_ids,
    policy_role_ids,
    resolve_policy,
)
from app.schemas.extraction import FetchConfig
from app.services import extraction_runs
from app.services.auto_onboarding_decision import (
    AutoOnboardingDecisionService,
    DecisionContext,
    snapshot_policy,
)
from app.services.auto_onboarding_execution import execute_decision
from app.services.auto_onboarding_persistence import record_decision
from app.services.website_configuration import save_draft_configuration
from app.services.websites import transition_website

# One probe, ever. Detail-page enrichment at extraction time is separately
# capped by `SiteConfiguration.max_detail_fetches`; this is only the single
# document inference is allowed to look at while deciding.
MAX_DETAIL_PROBES = 1

# Sample events are stored on `Website.proposed_pattern` as plain JSON so the
# review screen can show what extraction actually produced. They are copies of
# in-flight EventCandidates — no Event row is created by any of this.
MAX_SAMPLES_RECORDED = 5


def _sample_dict(candidate: EventCandidate) -> dict:
    return {
        "title": candidate.title,
        "start_date": candidate.start_date.isoformat() if candidate.start_date else None,
        "start_time": candidate.start_time.isoformat() if candidate.start_time else None,
        "canonical_url": candidate.canonical_url,
        "venue": candidate.venue,
        "image_url": candidate.image_url,
        "description": (candidate.description or "")[:200] or None,
        "warnings": list(candidate.warnings[:5]),
    }


@dataclass(frozen=True)
class AutoOnboardingResult:
    outcome: str
    detection: ExtractionResult
    inference: InferenceResult
    preview: ExtractionResult | None
    quality: PreviewQualityResult | None
    blocking_reasons: tuple[str, ...]
    valid_samples: tuple[EventCandidate, ...] = ()
    rejected_samples: tuple[tuple[EventCandidate, tuple[str, ...]], ...] = ()
    # Phase 8D. `decision` is the immutable policy evaluation; `execution`
    # records what, if anything, was done about it. Both are None when no
    # configuration was produced, since there is nothing to evaluate.
    decision: object | None = None
    execution: object | None = None

    def as_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "inference": self.inference.as_dict(),
            "quality": self.quality.as_dict() if self.quality else None,
            "blocking_reasons": list(self.blocking_reasons),
            "preview_status": self.preview.status if self.preview else None,
            "preview_run_id": self.preview.run_id if self.preview else None,
            "samples": {
                "valid": [_sample_dict(c) for c in self.valid_samples[:MAX_SAMPLES_RECORDED]],
                "rejected": [
                    {**_sample_dict(c), "errors": list(errors)}
                    for c, errors in self.rejected_samples[:MAX_SAMPLES_RECORDED]
                ],
            },
            "generated_at": datetime.now(UTC).isoformat(),
        }


def _fallback_timezone(website: Website) -> str | None:
    if website.timezone_override:
        return website.timezone_override
    if website.city is not None:
        return website.city.timezone
    return None


async def _probe_detail_document(url: str) -> str | None:
    """One bounded fetch through the same SSRF-protected strategy the
    extraction pipeline uses — not a separately-trusted code path."""
    fetch_config = FetchConfig()
    fetch = extraction_runs.HttpFetchStrategy()
    response = await fetch.fetch(FetchRequest(url=url), fetch_config)
    if response.blocked_reason is not None or response.status_code != 200:
        return None
    if not content_type_allowed(response.content_type, fetch_config):
        return None
    return response.text


def _record(website: Website, result: AutoOnboardingResult) -> None:
    proposed = dict(website.proposed_pattern or {})
    proposed["inference"] = result.as_dict()
    if result.inference.configuration is not None:
        proposed["configuration"] = result.inference.configuration.model_dump(mode="json")
    website.proposed_pattern = proposed


async def detect_and_configure(
    db: Session,
    website: Website,
    *,
    correlation_id: str | None = None,
    policy: AutoOnboardingPolicy = DEFAULT_POLICY,
    submitted_by_user_id: int | None = None,
    onboarding_job_id: int | None = None,
    onboarding_batch_id: int | None = None,
    submitter_role_ids: frozenset[int] = frozenset(),
) -> AutoOnboardingResult:
    detection_outcome = await extraction_runs.run_detection_detailed(
        db, website, correlation_id=correlation_id
    )
    service = ConfigurationInferenceService(REGISTRY, policy=policy)
    context = service.build_context(
        response=detection_outcome.response,
        detection=detection_outcome.detection,
        listing_url=detection_outcome.listing_url,
        fallback_timezone=_fallback_timezone(website),
    )

    proposal = service.propose(context)
    if isinstance(proposal, InferenceResult):
        # Blocked / browser-required / unsupported / no proposer: nothing to
        # preview, and nothing is written to the draft configuration.
        return _finish(db, website, detection_outcome.result, proposal, None, None, ())

    proposal = await _resolve_detail_probe(service, context, proposal)
    inference = service.finalize(proposal, detection_outcome.detection)
    if inference.configuration is None:
        return _finish(db, website, detection_outcome.result, inference, None, None, ())

    save_draft_configuration(db, website, inference.configuration)
    # Record how this draft was produced, so an automatic-approval policy can
    # refuse to approve anything it did not deterministically derive itself.
    website.configuration_origin = (
        ORIGIN_DETERMINISTIC_GENERIC_HTML
        if inference.configuration.pattern_name == "generic_html_cards"
        else ORIGIN_DETERMINISTIC_STRUCTURED
    )
    db.commit()

    preview = await extraction_runs.preview_extraction_detailed(
        db, website, correlation_id=correlation_id
    )

    ok, reasons = meets_approval_bar(
        preview.quality, policy, preview_status=preview.result.status
    )
    if preview.result.status == "blocked":
        outcome = BLOCKED
    elif inference.missing_required_fields:
        # Fail closed, but *informatively*: a preview that produced nothing
        # because a required field could not be inferred is not an unexplained
        # failure — we know exactly what is missing and an administrator can
        # act on it. `failed` is reserved for the cases we can't explain.
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

    # --- Phase 8D: policy evaluation, then any action it permits ----------
    # Placed here, in the single orchestration path, so the bulk queue and the
    # single-site "Detect and configure" button both get it without either
    # re-running detection or preview to obtain a decision.
    decision, execution = await _evaluate_and_execute_policy(
        db,
        website,
        detection_status=detection_outcome.result.status,
        browser_required=detection_outcome.detection.browser_required,
        inference=inference,
        preview=preview,
        submitted_by_user_id=submitted_by_user_id,
        onboarding_job_id=onboarding_job_id,
        onboarding_batch_id=onboarding_batch_id,
        submitter_role_ids=submitter_role_ids,
        correlation_id=correlation_id,
    )
    if execution is not None:
        if execution.activated:
            outcome = AUTOMATICALLY_ACTIVATED
        elif execution.approved:
            outcome = AUTOMATICALLY_APPROVED

    return _finish(
        db,
        website,
        detection_outcome.result,
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


async def _evaluate_and_execute_policy(
    db: Session,
    website: Website,
    *,
    detection_status: str,
    browser_required: bool,
    inference: InferenceResult,
    preview,
    submitted_by_user_id: int | None,
    onboarding_job_id: int | None,
    onboarding_batch_id: int | None,
    submitter_role_ids: frozenset[int],
    correlation_id: str | None,
):
    """Resolves the applicable policy, records an immutable decision, and
    carries out whatever it permits. Always records a decision — including
    "no applicable policy" and every denial — because a source that did not
    qualify is exactly the case an administrator later needs to explain."""
    # A batch may carry an explicit policy override (whose selection was
    # permission-checked at submission time); otherwise resolution is the
    # ordinary city -> global default precedence.
    selected_policy_id = None
    if onboarding_batch_id is not None:
        batch = db.get(OnboardingBatch, onboarding_batch_id)
        selected_policy_id = batch.selected_policy_id if batch is not None else None
    policy = resolve_policy(db, city_id=website.city_id, selected_policy_id=selected_policy_id)
    snapshot = (
        snapshot_policy(
            policy,
            city_ids=policy_city_ids(db, policy.id),
            role_ids=policy_role_ids(db, policy.id),
        )
        if policy is not None
        else None
    )

    preview_run = (
        db.get(ExtractionRun, preview.result.run_id) if preview.result.run_id else None
    )
    context = DecisionContext(
        policy=snapshot,
        website_id=website.id,
        website_is_archived=website.archived_at is not None,
        website_onboarding_status=website.onboarding_status,
        city_id=website.city_id,
        city_is_active=bool(website.city and website.city.is_active),
        detected_pattern=inference.pattern_name,
        detector_confidence=inference.detection_confidence,
        detection_status=detection_status,
        browser_required=browser_required,
        blocked=preview.result.status == "blocked",
        registered_patterns=frozenset(REGISTRY.names()),
        configuration=inference.configuration,
        configuration_origin=website.configuration_origin,
        configuration_version=website.configuration_version,
        preview_run_id=preview.result.run_id,
        preview_status=preview.result.status,
        preview_configuration_version=(
            preview_run.configuration_version if preview_run else None
        ),
        quality=preview.quality,
        warnings=tuple(preview.result.warnings),
        missing_required_fields=tuple(inference.missing_required_fields),
        field_candidates=tuple(c.as_dict() for c in inference.field_candidates),
        date_format_candidates=tuple(c.as_dict() for c in inference.date_format_candidates),
        detail_enrichment_used=bool(preview.quality and preview.quality.detail_fetch_used),
        submitted_by_user_id=submitted_by_user_id,
        submitter_role_ids=submitter_role_ids,
    )

    result = AutoOnboardingDecisionService().evaluate(context)
    decision = record_decision(
        db,
        result,
        website=website,
        onboarding_job_id=onboarding_job_id,
        onboarding_batch_id=onboarding_batch_id,
        submitted_by_user_id=submitted_by_user_id,
        correlation_id=correlation_id,
    )
    execution = execute_decision(db, decision, website=website, correlation_id=correlation_id)
    return decision, execution


async def _resolve_detail_probe(
    service: ConfigurationInferenceService,
    context,
    proposal: ConfigurationProposal,
) -> ConfigurationProposal:
    """A proposer that returned a `detail_probe_url` is telling us the listing
    page alone can't yield a required value (in practice: a card date with no
    year). Fetch exactly one document and let it propose again."""
    probes = 0
    while (
        proposal.detail_probe_url
        and proposal.configuration is None
        and probes < MAX_DETAIL_PROBES
    ):
        probes += 1
        document = await _probe_detail_document(proposal.detail_probe_url)
        if document is None:
            return ConfigurationProposal(
                configuration=None,
                field_candidates=proposal.field_candidates,
                warnings=(*proposal.warnings, "detail_page_probe_failed"),
                notes=proposal.notes,
                error="the detail page needed to resolve a complete date could not be fetched",
            )
        context = service.build_context(
            response=context.response,
            detection=context.detection,
            listing_url=context.listing_url,
            fallback_timezone=context.fallback_timezone,
            detail_documents={**context.detail_documents, proposal.detail_probe_url: document},
        )
        proposal = service.propose(context)
        if isinstance(proposal, InferenceResult):  # pragma: no cover - defensive
            return ConfigurationProposal(configuration=None, error=proposal.error)
    return proposal


def _finish(
    db: Session,
    website: Website,
    detection: ExtractionResult,
    inference: InferenceResult,
    preview: ExtractionResult | None,
    quality: PreviewQualityResult | None,
    blocking: tuple[str, ...],
    *,
    valid_samples: tuple[EventCandidate, ...] = (),
    rejected_samples: tuple[tuple[EventCandidate, tuple[str, ...]], ...] = (),
    outcome_override: str | None = None,
    decision=None,
    execution=None,
) -> AutoOnboardingResult:
    outcome = outcome_override or inference.outcome
    result = AutoOnboardingResult(
        outcome=outcome,
        detection=detection,
        inference=inference,
        preview=preview,
        quality=quality,
        blocking_reasons=blocking,
        valid_samples=valid_samples,
        rejected_samples=rejected_samples,
        decision=decision,
        execution=execution,
    )
    _record(website, result)

    # An automatically-configured source that didn't clear the bar belongs in
    # the review queue. Approval itself stays a separate, permissioned action
    # — nothing here ever approves or activates a website.
    if outcome == NEEDS_REVIEW and can_transition(
        website.onboarding_status, ONBOARDING_NEEDS_REVIEW
    ):
        transition_website(db, website, ONBOARDING_NEEDS_REVIEW)

    db.commit()
    db.refresh(website)
    return result
