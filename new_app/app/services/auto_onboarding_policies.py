"""Policy administration: create, edit, activate, assign.

Validation lives here rather than in the router so the same rules apply
whatever calls it. Notably:

* allowed pattern names are checked against PatternRegistry, so a policy can
  never allow a pattern that does not exist
* every threshold is range-checked, and percentages are bounded to 0..1
* enabling automatic approval or activation requires the caller to have
  passed the confirmation phrase — the service refuses without it, so the
  guard is not merely a UI affordance
* any meaningful change bumps `version`, and the audit entry records which
  fields changed
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.extraction.registry import REGISTRY
from app.models.auto_onboarding_policy import (
    AutoOnboardingPolicy,
    AutoOnboardingPolicyCity,
    AutoOnboardingPolicyRole,
)
from app.models.city import City
from app.models.role import Role
from app.services.audit import record_audit

APPROVAL_CONFIRMATION_PHRASE = "ENABLE AUTO APPROVAL"
ACTIVATION_CONFIRMATION_PHRASE = "ENABLE AUTO ACTIVATION"

# Fields whose change is "meaningful" and therefore bumps the version. Name
# and description are excluded: renaming a policy does not change what it
# decides, and bumping the version would wrongly imply older decisions were
# made under different rules.
VERSIONED_FIELDS: tuple[str, ...] = (
    "active",
    "automatic_configuration_enabled",
    "automatic_preview_enabled",
    "automatic_approval_enabled",
    "automatic_activation_enabled",
    "allowed_pattern_names",
    "allow_generic_html_cards",
    "allow_browser_required",
    "allow_ai_origin",
    "allow_administrator_manual_origin",
    "allow_imported_configuration",
    "allow_detail_page_enrichment",
    "minimum_detector_confidence",
    "minimum_events_found",
    "minimum_valid_events",
    "minimum_valid_percentage",
    "maximum_rejected_percentage",
    "minimum_canonical_url_coverage",
    "minimum_start_date_coverage",
    "minimum_start_date_parse_success",
    "maximum_duplicate_rate",
    "maximum_warning_count",
    "maximum_critical_warning_count",
    "minimum_distinct_events",
    "require_absolute_public_urls",
    "require_zero_critical_warnings",
    "require_distinct_events",
    "generic_html_minimum_detector_confidence",
    "generic_html_minimum_events_found",
    "generic_html_minimum_valid_events",
    "generic_html_minimum_valid_percentage",
    "generic_html_maximum_rejected_percentage",
    "generic_html_minimum_required_field_confidence",
    "generic_html_minimum_required_field_coverage",
    "generic_html_minimum_date_format_confidence",
    "generic_html_minimum_distinct_event_count",
    "generic_html_reject_broad_canonical_selector",
    "generic_html_reject_unstable_required_selectors",
)

PERCENTAGE_FIELDS: frozenset[str] = frozenset(
    field
    for field in VERSIONED_FIELDS
    if field.endswith(("_percentage", "_coverage", "_confidence", "_rate", "_success"))
)
COUNT_FIELDS: frozenset[str] = frozenset(
    field
    for field in VERSIONED_FIELDS
    if field.endswith(("_count", "_events", "_found"))
)

BOOLEAN_FIELDS: frozenset[str] = frozenset(
    field
    for field in VERSIONED_FIELDS
    if field.startswith(("allow_", "automatic_", "require_", "generic_html_reject_"))
    or field == "active"
)


@dataclass(frozen=True)
class PolicyChange:
    policy: AutoOnboardingPolicy
    changed_fields: tuple[str, ...]
    version_bumped: bool


def validate_pattern_names(names) -> list[str]:
    registered = set(REGISTRY.names())
    cleaned: list[str] = []
    for name in names or ():
        value = str(name).strip()
        if not value:
            continue
        if value not in registered:
            raise AppError(f"'{value}' is not a registered extraction pattern", status_code=422)
        if value not in cleaned:
            cleaned.append(value)
    return cleaned


def validate_values(values: dict) -> dict:
    """Range-checks every numeric field. Percentages are bounded to 0..1 and
    counts must be non-negative — a policy with a 500% threshold would be
    silently unsatisfiable rather than obviously wrong."""
    cleaned = dict(values)
    for field in PERCENTAGE_FIELDS & cleaned.keys():
        try:
            number = float(cleaned[field])
        except (TypeError, ValueError) as exc:
            raise AppError(f"'{field}' must be a number", status_code=422) from exc
        if not 0.0 <= number <= 1.0:
            raise AppError(
                f"'{field}' must be between 0 and 1 (got {number})", status_code=422
            )
        cleaned[field] = number
    for field in COUNT_FIELDS & cleaned.keys():
        try:
            number = int(cleaned[field])
        except (TypeError, ValueError) as exc:
            raise AppError(f"'{field}' must be a whole number", status_code=422) from exc
        if number < 0:
            raise AppError(f"'{field}' cannot be negative", status_code=422)
        cleaned[field] = number
    if "allowed_pattern_names" in cleaned:
        cleaned["allowed_pattern_names"] = validate_pattern_names(cleaned["allowed_pattern_names"])
    return cleaned


def _require_confirmation(values: dict, current: AutoOnboardingPolicy | None, confirmations: dict):
    """Turning automatic approval or activation *on* needs an explicit typed
    phrase. Activation's is separate and stronger than approval's, because a
    qualifying source becoming publicly visible without review is a bigger
    step than one becoming approvable."""
    enabling_approval = values.get("automatic_approval_enabled") and not (
        current.automatic_approval_enabled if current else False
    )
    enabling_activation = values.get("automatic_activation_enabled") and not (
        current.automatic_activation_enabled if current else False
    )
    if enabling_approval and confirmations.get("approval") != APPROVAL_CONFIRMATION_PHRASE:
        raise AppError(
            f"To enable automatic approval, type '{APPROVAL_CONFIRMATION_PHRASE}' to confirm.",
            status_code=422,
        )
    if enabling_activation and confirmations.get("activation") != ACTIVATION_CONFIRMATION_PHRASE:
        raise AppError(
            f"To enable automatic activation, type '{ACTIVATION_CONFIRMATION_PHRASE}' to "
            "confirm. Qualifying sources may become publicly active without human review.",
            status_code=422,
        )


def create_policy(
    db: Session,
    *,
    name: str,
    description: str | None,
    values: dict,
    confirmations: dict | None = None,
    actor_id: int | None = None,
    correlation_id: str | None = None,
) -> AutoOnboardingPolicy:
    name = (name or "").strip()
    if not name:
        raise AppError("A policy name is required.", status_code=422)
    if db.query(AutoOnboardingPolicy).filter(AutoOnboardingPolicy.name == name).first():
        raise AppError(f"A policy named '{name}' already exists.", status_code=422)

    cleaned = validate_values(values)
    _require_confirmation(cleaned, None, confirmations or {})

    policy = AutoOnboardingPolicy(name=name, description=description or None, version=1)
    for field, value in cleaned.items():
        setattr(policy, field, value)
    policy.created_by_user_id = actor_id
    policy.updated_by_user_id = actor_id
    db.add(policy)
    db.commit()
    db.refresh(policy)

    record_audit(
        db,
        actor_id=actor_id,
        action="auto_onboarding_policy_created",
        entity_type="auto_onboarding_policy",
        entity_id=policy.id,
        after={"name": policy.name, "version": policy.version, **_summary(policy)},
        correlation_id=correlation_id,
    )
    return policy


def update_policy(
    db: Session,
    policy: AutoOnboardingPolicy,
    *,
    name: str | None = None,
    description: str | None = None,
    values: dict,
    confirmations: dict | None = None,
    actor_id: int | None = None,
    correlation_id: str | None = None,
) -> PolicyChange:
    cleaned = validate_values(values)
    _require_confirmation(cleaned, policy, confirmations or {})

    before = {field: getattr(policy, field) for field in VERSIONED_FIELDS}
    if name is not None and name.strip():
        policy.name = name.strip()
    if description is not None:
        policy.description = description or None
    for field, value in cleaned.items():
        setattr(policy, field, value)

    changed = tuple(
        field for field in VERSIONED_FIELDS if before[field] != getattr(policy, field)
    )
    if changed:
        # Historical decisions keep pointing at the version they were made
        # under; they are never rewritten.
        policy.version += 1
    policy.updated_by_user_id = actor_id
    db.commit()
    db.refresh(policy)

    record_audit(
        db,
        actor_id=actor_id,
        action="auto_onboarding_policy_updated",
        entity_type="auto_onboarding_policy",
        entity_id=policy.id,
        before={"version": policy.version - (1 if changed else 0)},
        after={"version": policy.version, "changed_fields": list(changed)},
        correlation_id=correlation_id,
    )
    return PolicyChange(policy=policy, changed_fields=changed, version_bumped=bool(changed))


def set_active(
    db: Session,
    policy: AutoOnboardingPolicy,
    *,
    active: bool,
    actor_id: int | None = None,
    correlation_id: str | None = None,
) -> AutoOnboardingPolicy:
    if policy.active == active:
        return policy
    policy.active = active
    policy.version += 1
    policy.updated_by_user_id = actor_id
    db.commit()
    db.refresh(policy)
    record_audit(
        db,
        actor_id=actor_id,
        action=(
            "auto_onboarding_policy_activated"
            if active
            else "auto_onboarding_policy_deactivated"
        ),
        entity_type="auto_onboarding_policy",
        entity_id=policy.id,
        after={"active": active, "version": policy.version},
        correlation_id=correlation_id,
    )
    return policy


def set_global_default(
    db: Session,
    policy: AutoOnboardingPolicy,
    *,
    actor_id: int | None = None,
    correlation_id: str | None = None,
) -> AutoOnboardingPolicy:
    """Promotes one policy to global default, demoting the incumbent first.

    The database also enforces at most one *active* global default (partial
    unique index); this keeps the transition from tripping it.
    """
    if not policy.active:
        raise AppError("A deactivated policy cannot be the global default.", status_code=409)
    for other in (
        db.query(AutoOnboardingPolicy)
        .filter(
            AutoOnboardingPolicy.is_global_default.is_(True),
            AutoOnboardingPolicy.id != policy.id,
        )
        .all()
    ):
        other.is_global_default = False
    db.flush()
    policy.is_global_default = True
    policy.updated_by_user_id = actor_id
    db.commit()
    db.refresh(policy)
    record_audit(
        db,
        actor_id=actor_id,
        action="auto_onboarding_policy_assigned_global",
        entity_type="auto_onboarding_policy",
        entity_id=policy.id,
        after={"is_global_default": True},
        correlation_id=correlation_id,
    )
    return policy


def assign_city(
    db: Session,
    policy: AutoOnboardingPolicy,
    *,
    city_id: int,
    actor_id: int | None = None,
    correlation_id: str | None = None,
) -> AutoOnboardingPolicyCity:
    if db.get(City, city_id) is None:
        raise AppError("That city does not exist.", status_code=422)
    existing = (
        db.query(AutoOnboardingPolicyCity)
        .filter(AutoOnboardingPolicyCity.city_id == city_id)
        .first()
    )
    if existing is not None and existing.policy_id != policy.id:
        raise AppError(
            "That city is already assigned to another policy. Remove that assignment first.",
            status_code=409,
        )
    if existing is not None:
        return existing

    assignment = AutoOnboardingPolicyCity(policy_id=policy.id, city_id=city_id)
    db.add(assignment)
    db.commit()
    db.refresh(assignment)
    record_audit(
        db,
        actor_id=actor_id,
        action="auto_onboarding_policy_assigned_city",
        entity_type="auto_onboarding_policy",
        entity_id=policy.id,
        after={"city_id": city_id},
        correlation_id=correlation_id,
    )
    return assignment


def remove_city(
    db: Session,
    policy: AutoOnboardingPolicy,
    *,
    city_id: int,
    actor_id: int | None = None,
    correlation_id: str | None = None,
) -> None:
    assignment = (
        db.query(AutoOnboardingPolicyCity)
        .filter(
            AutoOnboardingPolicyCity.policy_id == policy.id,
            AutoOnboardingPolicyCity.city_id == city_id,
        )
        .first()
    )
    if assignment is None:
        return
    db.delete(assignment)
    db.commit()
    record_audit(
        db,
        actor_id=actor_id,
        action="auto_onboarding_policy_removed_from_city",
        entity_type="auto_onboarding_policy",
        entity_id=policy.id,
        after={"city_id": city_id},
        correlation_id=correlation_id,
    )


def set_roles(
    db: Session,
    policy: AutoOnboardingPolicy,
    *,
    role_ids: list[int],
    actor_id: int | None = None,
    correlation_id: str | None = None,
) -> None:
    """Replaces the policy's role restriction. Roles are referenced by id;
    display names are never used to decide anything."""
    valid = {row.id for row in db.query(Role).all()}
    for role_id in role_ids:
        if role_id not in valid:
            raise AppError("Unknown role selected.", status_code=422)

    db.query(AutoOnboardingPolicyRole).filter(
        AutoOnboardingPolicyRole.policy_id == policy.id
    ).delete()
    for role_id in role_ids:
        db.add(AutoOnboardingPolicyRole(policy_id=policy.id, role_id=role_id))
    db.commit()
    record_audit(
        db,
        actor_id=actor_id,
        action="auto_onboarding_policy_updated",
        entity_type="auto_onboarding_policy",
        entity_id=policy.id,
        after={"role_ids": sorted(role_ids)},
        correlation_id=correlation_id,
    )


def _summary(policy: AutoOnboardingPolicy) -> dict:
    return {
        "active": policy.active,
        "automatic_approval_enabled": policy.automatic_approval_enabled,
        "automatic_activation_enabled": policy.automatic_activation_enabled,
        "allowed_pattern_names": list(policy.allowed_pattern_names or ()),
    }
