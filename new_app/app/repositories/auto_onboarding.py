"""Persistence for automatic-onboarding policies, decisions and action results.

Policy *resolution* lives here too, because it is a pure lookup: precedence is
city assignment, then active global default, then nothing. It never consults a
hostname, a site name, detector evidence text or submitted metadata — only the
city the Website is assigned to.

Batch-selected policy override is deferred within Phase 8D (it needs a column
on `onboarding_batches` and a policy selector in the submission form); the
resolver's signature leaves room for it rather than pretending it exists.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.auto_onboarding import (
    ACTOR_SYSTEM,
    DECISION_ONBOARDING,
    DEFAULT_POLICY_NAME,
)
from app.models.auto_onboarding_action_result import AutoOnboardingActionResult
from app.models.auto_onboarding_decision import AutoOnboardingDecision
from app.models.auto_onboarding_policy import (
    AutoOnboardingPolicy,
    AutoOnboardingPolicyCity,
    AutoOnboardingPolicyRole,
)

# The conservative default. Every automatic action is off; the source of truth
# for these values is here and in the migration's seed, which must agree.
CONSERVATIVE_DEFAULTS: dict[str, object] = {
    "description": (
        "Conservative default: automatic configuration and preview are enabled, "
        "automatic approval and activation are not. Installing automatic onboarding "
        "must not change any existing source's outcome."
    ),
    "active": True,
    "is_global_default": True,
    "automatic_configuration_enabled": True,
    "automatic_preview_enabled": True,
    "automatic_approval_enabled": False,
    "automatic_activation_enabled": False,
    "allowed_pattern_names": [],
    "allow_generic_html_cards": False,
    "allow_browser_required": False,
    "allow_ai_origin": False,
    "allow_administrator_manual_origin": False,
    "allow_imported_configuration": False,
    "allow_detail_page_enrichment": True,
}


# --- Policies ---------------------------------------------------------------


def get_policy(db: Session, policy_id: int) -> AutoOnboardingPolicy | None:
    return db.get(AutoOnboardingPolicy, policy_id)


def get_policy_by_name(db: Session, name: str) -> AutoOnboardingPolicy | None:
    return db.query(AutoOnboardingPolicy).filter(AutoOnboardingPolicy.name == name).first()


def list_policies(db: Session, *, active_only: bool = False) -> list[AutoOnboardingPolicy]:
    query = db.query(AutoOnboardingPolicy)
    if active_only:
        query = query.filter(AutoOnboardingPolicy.active.is_(True))
    return query.order_by(AutoOnboardingPolicy.name).all()


def global_default_policy(db: Session) -> AutoOnboardingPolicy | None:
    return (
        db.query(AutoOnboardingPolicy)
        .filter(
            AutoOnboardingPolicy.is_global_default.is_(True),
            AutoOnboardingPolicy.active.is_(True),
        )
        .order_by(AutoOnboardingPolicy.id)
        .first()
    )


def policy_for_city(db: Session, city_id: int | None) -> AutoOnboardingPolicy | None:
    """The city's assigned policy, if it has one and that policy is active.

    An inactive assigned policy does not silently fall through to the global
    default — see `resolve_policy`, which treats it as a deliberate "this city
    has no applicable policy" rather than quietly applying different rules.
    """
    if city_id is None:
        return None
    assignment = (
        db.query(AutoOnboardingPolicyCity)
        .filter(AutoOnboardingPolicyCity.city_id == city_id)
        .first()
    )
    if assignment is None:
        return None
    policy = db.get(AutoOnboardingPolicy, assignment.policy_id)
    return policy if policy is not None and policy.active else None


def resolve_policy(
    db: Session, *, city_id: int | None, selected_policy_id: int | None = None
) -> AutoOnboardingPolicy | None:
    """Precedence: batch-selected policy, then city assignment, then active
    global default, then None.

    A selected-but-inactive policy resolves to None rather than falling
    through — the same principle applied to a deactivated city policy:
    switching a chosen rule set off stops automatic action, it does not
    silently substitute a different one. (Authorization to *select* a policy
    that enables approval/activation is enforced at submission time; by the
    time resolution runs, the selection is a trusted input.)
    """
    if selected_policy_id is not None:
        selected = db.get(AutoOnboardingPolicy, selected_policy_id)
        return selected if selected is not None and selected.active else None
    if city_id is not None:
        assignment = (
            db.query(AutoOnboardingPolicyCity)
            .filter(AutoOnboardingPolicyCity.city_id == city_id)
            .first()
        )
        if assignment is not None:
            policy = db.get(AutoOnboardingPolicy, assignment.policy_id)
            return policy if policy is not None and policy.active else None
    return global_default_policy(db)


def policy_role_ids(db: Session, policy_id: int) -> list[int]:
    return [
        row.role_id
        for row in db.query(AutoOnboardingPolicyRole)
        .filter(AutoOnboardingPolicyRole.policy_id == policy_id)
        .order_by(AutoOnboardingPolicyRole.role_id)
        .all()
    ]


def policy_city_ids(db: Session, policy_id: int) -> list[int]:
    return [
        row.city_id
        for row in db.query(AutoOnboardingPolicyCity)
        .filter(AutoOnboardingPolicyCity.policy_id == policy_id)
        .order_by(AutoOnboardingPolicyCity.city_id)
        .all()
    ]


def ensure_default_policy(db: Session) -> AutoOnboardingPolicy:
    """Idempotent. Safe to call from the seeder and from tests; never edits an
    existing policy, so an administrator who has since changed the defaults
    does not have their changes reverted on the next call."""
    existing = get_policy_by_name(db, DEFAULT_POLICY_NAME)
    if existing is not None:
        return existing
    policy = AutoOnboardingPolicy(name=DEFAULT_POLICY_NAME, **CONSERVATIVE_DEFAULTS)
    db.add(policy)
    db.commit()
    db.refresh(policy)
    return policy


# --- Decisions (append-only) ------------------------------------------------


def create_decision(db: Session, **values) -> AutoOnboardingDecision:
    """Inserts one evaluation snapshot. There is deliberately no update
    counterpart: an evaluation is a record of what was concluded at a moment,
    and what happened next belongs in an action result."""
    values.setdefault("decision_kind", DECISION_ONBOARDING)
    values.setdefault("system_actor_type", ACTOR_SYSTEM)
    decision = AutoOnboardingDecision(**values)
    db.add(decision)
    db.commit()
    db.refresh(decision)
    return decision


def get_decision(db: Session, decision_id: int) -> AutoOnboardingDecision | None:
    return db.get(AutoOnboardingDecision, decision_id)


def latest_decision_for_website(db: Session, website_id: int) -> AutoOnboardingDecision | None:
    return (
        db.query(AutoOnboardingDecision)
        .filter(AutoOnboardingDecision.website_id == website_id)
        .order_by(AutoOnboardingDecision.id.desc())
        .first()
    )


def list_decisions_for_website(
    db: Session, website_id: int, *, limit: int = 20
) -> list[AutoOnboardingDecision]:
    return (
        db.query(AutoOnboardingDecision)
        .filter(AutoOnboardingDecision.website_id == website_id)
        .order_by(AutoOnboardingDecision.id.desc())
        .limit(limit)
        .all()
    )


def list_decisions_for_policy(
    db: Session, policy_id: int, *, limit: int = 50
) -> list[AutoOnboardingDecision]:
    return (
        db.query(AutoOnboardingDecision)
        .filter(AutoOnboardingDecision.policy_id == policy_id)
        .order_by(AutoOnboardingDecision.id.desc())
        .limit(limit)
        .all()
    )


def count_decisions_for_policy(db: Session, policy_id: int) -> int:
    return (
        db.query(AutoOnboardingDecision)
        .filter(AutoOnboardingDecision.policy_id == policy_id)
        .count()
    )


def decision_for_job(db: Session, job_id: int) -> AutoOnboardingDecision | None:
    return (
        db.query(AutoOnboardingDecision)
        .filter(AutoOnboardingDecision.onboarding_job_id == job_id)
        .order_by(AutoOnboardingDecision.id.desc())
        .first()
    )


# --- Action results ---------------------------------------------------------


def create_action_result(db: Session, **values) -> AutoOnboardingActionResult:
    result = AutoOnboardingActionResult(**values)
    db.add(result)
    db.commit()
    db.refresh(result)
    return result


def action_results_for_decision(
    db: Session, decision_id: int
) -> list[AutoOnboardingActionResult]:
    return (
        db.query(AutoOnboardingActionResult)
        .filter(AutoOnboardingActionResult.decision_id == decision_id)
        .order_by(AutoOnboardingActionResult.id)
        .all()
    )
