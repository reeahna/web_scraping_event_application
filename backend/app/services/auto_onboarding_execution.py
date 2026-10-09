"""Acting on a decision: approval first, then — separately — activation.

The decision service concluded what is *allowed*. This module is the only
place that makes any of it happen, and it does so exclusively through the
existing services:

* approval goes through `website_configuration.approve_configuration`, so the
  stale-preview check, the configuration-version check, the browser-required
  refusal and the active-city requirement all still run. Nothing here writes
  `approved_pattern`, `approved_at`, `onboarding_status` or `is_active`.
* activation goes through `websites.transition_website`, so the lifecycle
  table and its activation preconditions still run.

Approval and activation are separate service calls, separate audit actions and
separate action-result rows. Activation eligibility is deliberately
re-evaluated *after* approval succeeds against freshly reloaded state — the
decision's `eligible_for_automatic_activation` is a policy statement made
before anything happened, not a licence to act later.

Partial success is never hidden: an approval that succeeds followed by an
activation that fails leaves the website approved and inactive, with one
succeeded row and one failed row against an unmutated decision.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.auto_onboarding import (
    ACTION_ACTIVATION,
    ACTION_APPROVAL,
    ACTION_FAILED,
    ACTOR_SYSTEM,
    AUTOMATIC_ACTIVATION_DENIED,
    AUTOMATICALLY_ACTIVATED,
    AUTOMATICALLY_APPROVED,
    SYSTEM_ACTOR_LABEL,
)
from app.core.onboarding import ACTIVE, can_transition
from app.models.auto_onboarding_action_result import AutoOnboardingActionResult
from app.models.auto_onboarding_decision import AutoOnboardingDecision
from app.models.website import Website
from app.repositories.auto_onboarding import create_action_result, get_policy
from app.services.audit import record_system_audit
from app.services.notifications import SEVERITY_ERROR, build_dedup_fingerprint, notify
from app.services.rbac import users_with_permission
from app.services.website_configuration import approve_configuration
from app.services.websites import transition_website


@dataclass(frozen=True)
class ExecutionOutcome:
    """What actually happened. `final_decision` is derived from the action
    results, never written back onto the decision row."""

    decision: AutoOnboardingDecision
    approval: AutoOnboardingActionResult | None = None
    activation: AutoOnboardingActionResult | None = None

    @property
    def approved(self) -> bool:
        return bool(self.approval and self.approval.succeeded)

    @property
    def activated(self) -> bool:
        return bool(self.activation and self.activation.succeeded)

    @property
    def final_decision(self) -> str:
        if self.activated:
            return AUTOMATICALLY_ACTIVATED
        if self.approved:
            # Approved but not activated is a complete, correct outcome when
            # activation is disabled — and an honest partial one when an
            # activation attempt failed.
            if self.activation is not None and not self.activation.succeeded:
                return AUTOMATICALLY_APPROVED
            return AUTOMATICALLY_APPROVED
        if self.approval is not None and not self.approval.succeeded:
            return ACTION_FAILED
        return self.decision.final_decision


def effective_decision(decision: AutoOnboardingDecision) -> str:
    """The decision's outcome including anything that was done about it.
    Reads the action-result rows rather than a mutated column."""
    results = {r.action_type: r for r in decision.action_results}
    activation = results.get(ACTION_ACTIVATION)
    approval = results.get(ACTION_APPROVAL)
    if activation is not None and activation.succeeded:
        return AUTOMATICALLY_ACTIVATED
    if approval is not None and approval.succeeded:
        return AUTOMATICALLY_APPROVED
    if approval is not None:
        return ACTION_FAILED
    return decision.final_decision


def _review_recipients(db: Session) -> list:
    return users_with_permission(db, "sites.approve")


def execute_decision(
    db: Session,
    decision: AutoOnboardingDecision,
    *,
    website: Website,
    correlation_id: str | None = None,
) -> ExecutionOutcome:
    """Carries out whatever the decision permits. Safe to call for a denied
    decision — it simply does nothing."""
    if not decision.eligible_for_automatic_approval:
        return ExecutionOutcome(decision=decision)

    approval = _attempt_approval(db, decision, website=website, correlation_id=correlation_id)
    if not approval.succeeded:
        # An approval that failed can never be followed by activation.
        return ExecutionOutcome(decision=decision, approval=approval)

    if not decision.activation_policy_enabled:
        return ExecutionOutcome(decision=decision, approval=approval)

    activation = _attempt_activation(
        db, decision, website=website, correlation_id=correlation_id
    )
    return ExecutionOutcome(decision=decision, approval=approval, activation=activation)


# --- approval ---------------------------------------------------------------


def _attempt_approval(
    db: Session,
    decision: AutoOnboardingDecision,
    *,
    website: Website,
    correlation_id: str | None,
) -> AutoOnboardingActionResult:
    try:
        # approved_by_user_id=None: no human approved this. The audit entry
        # below carries the system actor, and the decision row carries the
        # submitting user, so neither is misrepresented as the approver.
        approve_configuration(db, website, approved_by_user_id=None)
    except Exception as exc:
        db.rollback()
        db.refresh(website)
        error = f"{type(exc).__name__}: {exc}"[:500]
        audit = record_system_audit(
            db,
            action="automatic_approval_failed",
            entity_type="website",
            entity_id=website.id,
            after={
                "decision_id": decision.id,
                "policy_id": decision.policy_id,
                "policy_version": decision.policy_version,
                "configuration_version": decision.configuration_version,
                "error": error,
            },
            correlation_id=correlation_id,
        )
        _notify_action_failure(
            db,
            website=website,
            title=f"{website.name}: automatic approval failed",
            message=(
                f"'{website.name}' qualified for automatic approval under policy "
                f"#{decision.policy_id} but the approval could not be completed: {error}"
            ),
            fingerprint_parts=("automatic_approval_failed", str(website.id), str(decision.id)),
            correlation_id=correlation_id,
        )
        return create_action_result(
            db,
            decision_id=decision.id,
            website_id=website.id,
            action_type=ACTION_APPROVAL,
            attempted=True,
            succeeded=False,
            error=error,
            audit_log_id=audit.id,
            configuration_version=decision.configuration_version,
            actor_type=ACTOR_SYSTEM,
            actor_label=SYSTEM_ACTOR_LABEL,
        )

    db.refresh(website)
    audit = record_system_audit(
        db,
        action="website_automatically_approved",
        entity_type="website",
        entity_id=website.id,
        after={
            "decision_id": decision.id,
            "policy_id": decision.policy_id,
            "policy_version": decision.policy_version,
            "configuration_version": website.active_configuration_version,
            "preview_run_id": decision.preview_run_id,
            "detected_pattern": decision.detected_pattern,
            "detector_confidence": decision.detector_confidence,
            "submitted_by_user_id": decision.submitted_by_user_id,
            "is_active": website.is_active,
        },
        correlation_id=correlation_id,
    )
    return create_action_result(
        db,
        decision_id=decision.id,
        website_id=website.id,
        action_type=ACTION_APPROVAL,
        attempted=True,
        succeeded=True,
        audit_log_id=audit.id,
        configuration_version=website.active_configuration_version,
        actor_type=ACTOR_SYSTEM,
        actor_label=SYSTEM_ACTOR_LABEL,
    )


# --- activation -------------------------------------------------------------


def _activation_blockers(
    db: Session, decision: AutoOnboardingDecision, website: Website
) -> list[str]:
    """The post-approval re-check, against freshly reloaded state.

    Everything here was also true when the decision was made; it is checked
    again because approval itself changed the website, and because time passed
    in between.
    """
    blockers: list[str] = []
    if website.archived_at is not None:
        blockers.append("the website was archived")
    if not website.approved_pattern:
        blockers.append("the website has no approved configuration")
    if website.city is None or not website.city.is_active:
        blockers.append("the website's city is not active")
    if (
        decision.configuration_version is not None
        and website.active_configuration_version != decision.configuration_version
    ):
        blockers.append(
            f"the approved configuration is version {website.active_configuration_version}, "
            f"not the evaluated version {decision.configuration_version}"
        )
    if not can_transition(website.onboarding_status, ACTIVE):
        blockers.append(
            f"activation is not a legal transition from '{website.onboarding_status}'"
        )

    policy = get_policy(db, decision.policy_id) if decision.policy_id else None
    if policy is None or not policy.active:
        blockers.append("the policy is no longer active")
    elif not policy.automatic_activation_enabled:
        blockers.append("the policy no longer enables automatic activation")
    elif policy.version != decision.policy_version:
        blockers.append(
            f"the policy changed to version {policy.version} after this decision was made"
        )
    return blockers


def _attempt_activation(
    db: Session,
    decision: AutoOnboardingDecision,
    *,
    website: Website,
    correlation_id: str | None,
) -> AutoOnboardingActionResult:
    db.refresh(website)
    blockers = _activation_blockers(db, decision, website)
    if blockers:
        reason = "; ".join(blockers)[:500]
        audit = record_system_audit(
            db,
            action="automatic_activation_failed",
            entity_type="website",
            entity_id=website.id,
            after={"decision_id": decision.id, "not_attempted_because": reason},
            correlation_id=correlation_id,
        )
        return create_action_result(
            db,
            decision_id=decision.id,
            website_id=website.id,
            action_type=ACTION_ACTIVATION,
            # Not attempted: the re-check refused before any transition was
            # tried, which is a different fact from "we tried and it failed".
            attempted=False,
            succeeded=False,
            error=reason,
            audit_log_id=audit.id,
            configuration_version=website.active_configuration_version,
            actor_type=ACTOR_SYSTEM,
            actor_label=SYSTEM_ACTOR_LABEL,
        )

    try:
        transition_website(db, website, ACTIVE)
    except Exception as exc:
        db.rollback()
        db.refresh(website)
        error = f"{type(exc).__name__}: {exc}"[:500]
        audit = record_system_audit(
            db,
            action="automatic_activation_failed",
            entity_type="website",
            entity_id=website.id,
            after={"decision_id": decision.id, "error": error, "approval_preserved": True},
            correlation_id=correlation_id,
        )
        _notify_action_failure(
            db,
            website=website,
            title=f"{website.name}: automatic activation failed",
            message=(
                f"'{website.name}' was automatically approved but could not be activated: "
                f"{error}. The approved configuration is unchanged and the source remains "
                "inactive."
            ),
            fingerprint_parts=("automatic_activation_failed", str(website.id), str(decision.id)),
            correlation_id=correlation_id,
        )
        return create_action_result(
            db,
            decision_id=decision.id,
            website_id=website.id,
            action_type=ACTION_ACTIVATION,
            attempted=True,
            succeeded=False,
            error=error,
            audit_log_id=audit.id,
            configuration_version=website.active_configuration_version,
            actor_type=ACTOR_SYSTEM,
            actor_label=SYSTEM_ACTOR_LABEL,
        )

    db.refresh(website)
    audit = record_system_audit(
        db,
        action="website_automatically_activated",
        entity_type="website",
        entity_id=website.id,
        after={
            "decision_id": decision.id,
            "policy_id": decision.policy_id,
            "policy_version": decision.policy_version,
            "configuration_version": website.active_configuration_version,
            "onboarding_status": website.onboarding_status,
            "is_active": website.is_active,
        },
        correlation_id=correlation_id,
    )
    return create_action_result(
        db,
        decision_id=decision.id,
        website_id=website.id,
        action_type=ACTION_ACTIVATION,
        attempted=True,
        succeeded=True,
        audit_log_id=audit.id,
        configuration_version=website.active_configuration_version,
        actor_type=ACTOR_SYSTEM,
        actor_label=SYSTEM_ACTOR_LABEL,
    )


def _notify_action_failure(
    db: Session,
    *,
    website: Website,
    title: str,
    message: str,
    fingerprint_parts: tuple[str, ...],
    correlation_id: str | None,
) -> None:
    notify(
        db,
        notification_type=fingerprint_parts[0],
        severity=SEVERITY_ERROR,
        title=title,
        message=message,
        recipients=_review_recipients(db),
        related_resource_type="website",
        related_resource_id=website.id,
        action_url=f"/admin/websites/{website.id}",
        dedup_fingerprint=build_dedup_fingerprint(*fingerprint_parts),
        correlation_id=correlation_id,
    )


__all__ = [
    "AUTOMATIC_ACTIVATION_DENIED",
    "ExecutionOutcome",
    "effective_decision",
    "execute_decision",
]
