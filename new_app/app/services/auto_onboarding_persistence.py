"""Turning a pure decision result into an append-only decision row.

Kept apart from the decision service so that service stays free of a Session,
and apart from the execution service (increment 3) so that recording *what was
concluded* never depends on whether anything was then done about it.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.auto_onboarding import DECISION_ONBOARDING, SYSTEM_ACTOR_LABEL
from app.models.auto_onboarding_decision import AutoOnboardingDecision
from app.models.website import Website
from app.repositories.auto_onboarding import create_decision
from app.services.audit import record_system_audit
from app.services.auto_onboarding_decision import AutoOnboardingDecisionResult


def record_decision(
    db: Session,
    result: AutoOnboardingDecisionResult,
    *,
    website: Website,
    decision_kind: str = DECISION_ONBOARDING,
    onboarding_job_id: int | None = None,
    onboarding_batch_id: int | None = None,
    submitted_by_user_id: int | None = None,
    reevaluates_decision_id: int | None = None,
    correlation_id: str | None = None,
) -> AutoOnboardingDecision:
    """Inserts one immutable evaluation snapshot and audits it.

    Every evaluation is recorded, including denials — a source that did not
    qualify is exactly the case an administrator later needs to understand.
    """
    decision = create_decision(
        db,
        website_id=website.id,
        onboarding_job_id=onboarding_job_id,
        onboarding_batch_id=onboarding_batch_id,
        policy_id=result.policy_id,
        policy_version=result.policy_version,
        decision_kind=decision_kind,
        final_decision=result.final_decision,
        eligible_for_automatic_approval=result.eligible_for_automatic_approval,
        eligible_for_automatic_activation=result.eligible_for_automatic_activation,
        activation_policy_enabled=result.activation_policy_enabled,
        detected_pattern=result.detected_pattern,
        detector_confidence=result.detector_confidence,
        configuration_origin=result.configuration_origin,
        configuration_version=result.configuration_version,
        preview_run_id=result.preview_run_id,
        preview_status=result.preview_status,
        metrics_snapshot=result.metrics_snapshot,
        thresholds_snapshot=result.thresholds_snapshot,
        reasons_passed=list(result.reasons_passed),
        reasons_failed=list(result.reasons_failed),
        submitted_by_user_id=submitted_by_user_id,
        evaluated_roles=list(result.evaluated_roles) or None,
        reevaluates_decision_id=reevaluates_decision_id,
    )

    record_system_audit(
        db,
        action="auto_onboarding_decision_created",
        entity_type="website",
        entity_id=website.id,
        after={
            "decision_id": decision.id,
            "policy_id": result.policy_id,
            "policy_version": result.policy_version,
            "final_decision": result.final_decision,
            "eligible_for_automatic_approval": result.eligible_for_automatic_approval,
            "detected_pattern": result.detected_pattern,
            "configuration_version": result.configuration_version,
            "preview_run_id": result.preview_run_id,
            "reasons_failed": list(result.reasons_failed[:10]),
            "submitted_by_user_id": submitted_by_user_id,
        },
        correlation_id=correlation_id,
        actor_label=SYSTEM_ACTOR_LABEL,
    )
    return decision
