"""Re-running the policy against a website that has already been evaluated.

Append-only: a re-evaluation never edits the earlier decision, it inserts a
new one linked by `reevaluates_decision_id`. The old snapshot keeps saying
what was true under the policy version that produced it.

Two things it deliberately does not do:

* **Never re-detects or reevaluates an archived website.** Archived means
  resolved by a human; automatic machinery must leave it alone.
* **Never deactivates a live website.** A stricter policy produces a new
  decision recording that the source would no longer qualify — a
  recommendation for review, not an automatic shutdown. Turning a live source
  off stays a deliberate administrator action.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.auto_onboarding import DECISION_REEVALUATION
from app.core.exceptions import AppError
from app.extraction.registry import REGISTRY
from app.models.auto_onboarding_decision import AutoOnboardingDecision
from app.models.extraction_run import ExtractionRun
from app.models.website import Website
from app.repositories.auto_onboarding import (
    latest_decision_for_website,
    policy_city_ids,
    policy_role_ids,
    resolve_policy,
)
from app.repositories.extraction_run import get_latest_run_for_website
from app.schemas.extraction import SiteConfiguration
from app.services import extraction_runs
from app.services.audit import record_system_audit
from app.services.auto_onboarding_decision import (
    AutoOnboardingDecisionService,
    DecisionContext,
    snapshot_policy,
)
from app.services.auto_onboarding_execution import ExecutionOutcome, execute_decision
from app.services.auto_onboarding_persistence import record_decision

_ACCEPTABLE_PREVIEW_STATUSES = ("success", "partial")


@dataclass(frozen=True)
class ReevaluationResult:
    decision: AutoOnboardingDecision
    execution: ExecutionOutcome
    preview_reused: bool
    previous_decision_id: int | None


def _reusable_preview(db: Session, website: Website) -> ExtractionRun | None:
    """The latest preview, but only if it ran against the current draft. A
    stale preview is never reused — that is the same rule manual approval
    enforces, and re-evaluation must not become a way around it."""
    latest = get_latest_run_for_website(db, website.id, run_type="preview")
    if latest is None:
        return None
    if latest.configuration_version != website.configuration_version:
        return None
    if latest.status not in _ACCEPTABLE_PREVIEW_STATUSES:
        return None
    return latest


async def reevaluate_website(
    db: Session,
    website: Website,
    *,
    actor_id: int | None = None,
    correlation_id: str | None = None,
) -> ReevaluationResult:
    if website.archived_at is not None:
        raise AppError(
            "An archived website cannot be re-evaluated for automatic action.", status_code=409
        )
    if not website.configuration:
        raise AppError(
            "This website has no draft configuration to evaluate.", status_code=409
        )

    stored = (website.proposed_pattern or {}).get("inference") or {}
    inference = stored.get("inference") or {}

    preview_run = _reusable_preview(db, website)
    preview_reused = preview_run is not None
    quality = None
    warnings: tuple[str, ...] = ()
    preview_status = preview_run.status if preview_run else None

    if preview_run is None:
        # No usable preview: run one through the ordinary workflow rather than
        # evaluating against numbers that no longer describe this draft.
        preview = await extraction_runs.preview_extraction_detailed(
            db, website, correlation_id=correlation_id
        )
        quality = preview.quality
        warnings = tuple(preview.result.warnings)
        preview_status = preview.result.status
        preview_run = db.get(ExtractionRun, preview.result.run_id)
    else:
        stored_quality = stored.get("quality")
        if stored_quality:
            from app.extraction.inference.types import PreviewQualityResult

            quality = PreviewQualityResult(
                candidates_found=stored_quality.get("candidates_found", 0),
                valid_count=stored_quality.get("valid_count", 0),
                rejected_count=stored_quality.get("rejected_count", 0),
                valid_percentage=stored_quality.get("valid_percentage", 0.0),
                rejected_percentage=stored_quality.get("rejected_percentage", 0.0),
                required_field_coverage=stored_quality.get("required_field_coverage", {}),
                date_parse_success_rate=stored_quality.get("date_parse_success_rate", 0.0),
                url_validity_rate=stored_quality.get("url_validity_rate", 0.0),
                duplicate_rate=stored_quality.get("duplicate_rate", 0.0),
                warning_count=stored_quality.get("warning_count", 0),
                pagination_truncated=stored_quality.get("pagination_truncated", False),
                detail_fetch_used=stored_quality.get("detail_fetch_used", False),
                pages_fetched=stored_quality.get("pages_fetched", 0),
            )

    policy = resolve_policy(db, city_id=website.city_id)
    snapshot = (
        snapshot_policy(
            policy,
            city_ids=policy_city_ids(db, policy.id),
            role_ids=policy_role_ids(db, policy.id),
        )
        if policy is not None
        else None
    )

    configuration = SiteConfiguration.model_validate(website.configuration)
    context = DecisionContext(
        policy=snapshot,
        website_id=website.id,
        website_is_archived=website.archived_at is not None,
        website_onboarding_status=website.onboarding_status,
        city_id=website.city_id,
        city_is_active=bool(website.city and website.city.is_active),
        detected_pattern=configuration.pattern_name,
        detector_confidence=inference.get("detection_confidence", 0.0),
        detection_status="success",
        browser_required=bool(
            (website.proposed_pattern or {}).get("detection", {}).get("browser_required")
        ),
        blocked=False,
        registered_patterns=frozenset(REGISTRY.names()),
        configuration=configuration,
        configuration_origin=website.configuration_origin,
        configuration_version=website.configuration_version,
        preview_run_id=preview_run.id if preview_run else None,
        preview_status=preview_status,
        preview_configuration_version=(
            preview_run.configuration_version if preview_run else None
        ),
        quality=quality,
        warnings=warnings,
        missing_required_fields=tuple(inference.get("missing_required_fields") or ()),
        field_candidates=tuple(inference.get("field_candidates") or ()),
        date_format_candidates=tuple(inference.get("date_format_candidates") or ()),
        detail_enrichment_used=bool(quality and quality.detail_fetch_used),
    )

    previous = latest_decision_for_website(db, website.id)
    result = AutoOnboardingDecisionService().evaluate(context)
    decision = record_decision(
        db,
        result,
        website=website,
        decision_kind=DECISION_REEVALUATION,
        reevaluates_decision_id=previous.id if previous else None,
        correlation_id=correlation_id,
    )
    record_system_audit(
        db,
        action="auto_onboarding_reevaluated",
        entity_type="website",
        entity_id=website.id,
        after={
            "decision_id": decision.id,
            "previous_decision_id": previous.id if previous else None,
            "preview_reused": preview_reused,
            "policy_id": result.policy_id,
            "policy_version": result.policy_version,
            "requested_by_user_id": actor_id,
        },
        correlation_id=correlation_id,
    )

    was_active = website.is_active
    execution = execute_decision(db, decision, website=website, correlation_id=correlation_id)
    db.refresh(website)
    # A stricter policy must never turn a live source off.
    assert website.is_active or not was_active, "re-evaluation must never deactivate a website"

    return ReevaluationResult(
        decision=decision,
        execution=execution,
        preview_reused=preview_reused,
        previous_decision_id=previous.id if previous else None,
    )
