"""Increment 1: policy/decision/action-result models, conservative default,
policy resolution, and the audit system actor.

No approval or activation logic exists yet — these tests only establish that
the schema and its defaults are safe, and that installing the phase changes
nothing about how sources are onboarded today.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.auto_onboarding import (
    ACTOR_SYSTEM,
    ACTOR_USER,
    AUTOMATIC_APPROVAL_DENIED,
    DEFAULT_POLICY_NAME,
    SYSTEM_ACTOR_LABEL,
)
from app.models.audit_log import AuditLog
from app.models.auto_onboarding_action_result import AutoOnboardingActionResult
from app.models.auto_onboarding_decision import AutoOnboardingDecision
from app.models.auto_onboarding_policy import (
    AutoOnboardingPolicy,
    AutoOnboardingPolicyCity,
    AutoOnboardingPolicyRole,
)
from app.repositories.auto_onboarding import (
    action_results_for_decision,
    create_action_result,
    create_decision,
    ensure_default_policy,
    get_policy_by_name,
    global_default_policy,
    list_decisions_for_website,
    policy_for_city,
    resolve_policy,
)
from app.services.audit import record_audit, record_system_audit

# --- Conservative default ----------------------------------------------------


def test_the_default_policy_is_seeded(db_session):
    policy = get_policy_by_name(db_session, DEFAULT_POLICY_NAME)
    assert policy is not None
    assert policy.active is True
    assert policy.is_global_default is True
    assert policy.version == 1


def test_the_default_policy_disables_every_automatic_action(db_session):
    policy = get_policy_by_name(db_session, DEFAULT_POLICY_NAME)
    # Configuration and preview stay on — that is today's behaviour.
    assert policy.automatic_configuration_enabled is True
    assert policy.automatic_preview_enabled is True
    # Nothing may be approved or activated without a human.
    assert policy.automatic_approval_enabled is False
    assert policy.automatic_activation_enabled is False


def test_the_default_policy_denies_generic_html_browser_and_ai_origin(db_session):
    policy = get_policy_by_name(db_session, DEFAULT_POLICY_NAME)
    assert policy.allow_generic_html_cards is False
    assert policy.allow_browser_required is False
    assert policy.allow_ai_origin is False
    assert policy.allow_administrator_manual_origin is False
    assert policy.allow_imported_configuration is False
    assert policy.allowed_pattern_names == []


def test_the_default_policy_thresholds_are_conservative(db_session):
    policy = get_policy_by_name(db_session, DEFAULT_POLICY_NAME)
    assert policy.minimum_detector_confidence >= 0.8
    assert policy.minimum_valid_percentage >= 0.9
    assert policy.minimum_canonical_url_coverage == 1.0
    assert policy.minimum_start_date_coverage == 1.0
    assert policy.maximum_critical_warning_count == 0
    # generic_html_cards is stricter than the general set on every axis it
    # shares with it.
    assert (
        policy.generic_html_minimum_detector_confidence >= policy.minimum_detector_confidence
    )
    assert policy.generic_html_minimum_valid_events >= policy.minimum_valid_events
    assert policy.generic_html_minimum_valid_percentage >= policy.minimum_valid_percentage
    assert (
        policy.generic_html_maximum_rejected_percentage <= policy.maximum_rejected_percentage
    )


def test_seeding_the_default_policy_is_idempotent(db_session):
    first = ensure_default_policy(db_session)
    second = ensure_default_policy(db_session)
    assert first.id == second.id
    assert db_session.query(AutoOnboardingPolicy).count() == 1


def test_seeding_never_overwrites_administrator_changes(db_session):
    policy = ensure_default_policy(db_session)
    policy.automatic_approval_enabled = True
    policy.version = 2
    db_session.commit()

    ensure_default_policy(db_session)
    db_session.refresh(policy)
    assert policy.automatic_approval_enabled is True
    assert policy.version == 2


# --- Policy resolution -------------------------------------------------------


def test_resolution_falls_back_to_the_global_default(db_session, make_city):
    city = make_city(name="Fallback City", slug="fallback-city")
    resolved = resolve_policy(db_session, city_id=city.id)
    assert resolved.name == DEFAULT_POLICY_NAME


def test_a_city_assignment_takes_precedence_over_the_global_default(db_session, make_city):
    city = make_city(name="Scoped City", slug="scoped-city")
    scoped = AutoOnboardingPolicy(name="Scoped policy")
    db_session.add(scoped)
    db_session.commit()
    db_session.add(AutoOnboardingPolicyCity(policy_id=scoped.id, city_id=city.id))
    db_session.commit()

    assert resolve_policy(db_session, city_id=city.id).id == scoped.id
    assert policy_for_city(db_session, city.id).id == scoped.id
    # Other cities still get the global default.
    other = make_city(name="Other City", slug="other-city")
    assert resolve_policy(db_session, city_id=other.id).name == DEFAULT_POLICY_NAME


def test_a_deactivated_city_policy_does_not_fall_through_to_the_global_default(
    db_session, make_city
):
    """Deactivating a city's policy must stop automatic action for that city,
    not hand it a different rule set."""
    city = make_city(name="Paused City", slug="paused-city")
    scoped = AutoOnboardingPolicy(name="Paused policy", active=False)
    db_session.add(scoped)
    db_session.commit()
    db_session.add(AutoOnboardingPolicyCity(policy_id=scoped.id, city_id=city.id))
    db_session.commit()

    assert resolve_policy(db_session, city_id=city.id) is None


def test_a_city_can_only_be_assigned_one_policy(db_session, make_city):
    city = make_city(name="Single City", slug="single-city")
    first = AutoOnboardingPolicy(name="First policy")
    second = AutoOnboardingPolicy(name="Second policy")
    db_session.add_all([first, second])
    db_session.commit()
    db_session.add(AutoOnboardingPolicyCity(policy_id=first.id, city_id=city.id))
    db_session.commit()

    db_session.add(AutoOnboardingPolicyCity(policy_id=second.id, city_id=city.id))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_only_one_active_global_default_can_exist(db_session):
    rival = AutoOnboardingPolicy(name="Rival default", is_global_default=True, active=True)
    db_session.add(rival)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    assert global_default_policy(db_session).name == DEFAULT_POLICY_NAME


def test_a_deactivated_former_default_does_not_block_a_new_one(db_session):
    """The uniqueness guarantee is on *active* global defaults, so retiring
    one and promoting another is possible without deleting history."""
    current = global_default_policy(db_session)
    current.active = False
    db_session.commit()

    replacement = AutoOnboardingPolicy(name="New default", is_global_default=True, active=True)
    db_session.add(replacement)
    db_session.commit()
    assert global_default_policy(db_session).id == replacement.id


def test_role_assignments_are_unique_per_policy(db_session):
    policy = AutoOnboardingPolicy(name="Role scoped")
    db_session.add(policy)
    db_session.commit()
    role_id = db_session.query(AutoOnboardingPolicy).first().id  # any existing id works
    db_session.add(AutoOnboardingPolicyRole(policy_id=policy.id, role_id=role_id))
    db_session.commit()
    db_session.add(AutoOnboardingPolicyRole(policy_id=policy.id, role_id=role_id))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# --- Decisions and action results --------------------------------------------


@pytest.fixture
def website(db_session, make_city, make_website):
    city = make_city(name="Decision City", slug="decision-city")
    return make_website(city, name="Decision Site", base_url="https://decision.example.org")


def test_a_decision_records_its_evaluation_snapshot(db_session, website):
    policy = global_default_policy(db_session)
    decision = create_decision(
        db_session,
        website_id=website.id,
        policy_id=policy.id,
        policy_version=policy.version,
        final_decision=AUTOMATIC_APPROVAL_DENIED,
        detected_pattern="json_ld_event",
        detector_confidence=0.91,
        configuration_version=3,
        metrics_snapshot={"valid_percentage": 1.0},
        thresholds_snapshot={"minimum_valid_percentage": 0.9},
        reasons_passed=["valid percentage 100% >= 90%"],
        reasons_failed=["automatic approval is disabled by policy"],
    )
    assert decision.id is not None
    assert decision.system_actor_type == ACTOR_SYSTEM
    assert decision.eligible_for_automatic_approval is False
    assert decision.reasons_failed == ["automatic approval is disabled by policy"]


def test_reevaluation_appends_a_new_decision_and_preserves_the_old_one(db_session, website):
    first = create_decision(
        db_session,
        website_id=website.id,
        final_decision=AUTOMATIC_APPROVAL_DENIED,
        reasons_failed=["original reason"],
    )
    second = create_decision(
        db_session,
        website_id=website.id,
        final_decision=AUTOMATIC_APPROVAL_DENIED,
        reasons_failed=["new reason"],
        reevaluates_decision_id=first.id,
    )
    db_session.refresh(first)

    assert second.reevaluates_decision_id == first.id
    assert first.reasons_failed == ["original reason"], "history must not be rewritten"
    assert [d.id for d in list_decisions_for_website(db_session, website.id)] == [
        second.id,
        first.id,
    ]


def test_action_results_hang_off_a_decision_without_mutating_it(db_session, website):
    """The append-only split: a decision records what was concluded, action
    results record what happened, so partial success is visible as one
    succeeded row plus one failed row."""
    decision = create_decision(
        db_session,
        website_id=website.id,
        final_decision=AUTOMATIC_APPROVAL_DENIED,
        eligible_for_automatic_approval=True,
    )
    approval = create_action_result(
        db_session,
        decision_id=decision.id,
        website_id=website.id,
        action_type="approval",
        attempted=True,
        succeeded=True,
        configuration_version=4,
    )
    activation = create_action_result(
        db_session,
        decision_id=decision.id,
        website_id=website.id,
        action_type="activation",
        attempted=True,
        succeeded=False,
        error="city became inactive",
    )
    db_session.refresh(decision)

    results = action_results_for_decision(db_session, decision.id)
    assert [r.id for r in results] == [approval.id, activation.id]
    assert results[0].succeeded is True and results[1].succeeded is False
    assert results[0].actor_type == ACTOR_SYSTEM
    assert results[0].actor_label == SYSTEM_ACTOR_LABEL
    # The evaluation snapshot is untouched by either action.
    assert decision.eligible_for_automatic_approval is True
    assert decision.final_decision == AUTOMATIC_APPROVAL_DENIED


def test_deleting_a_decision_removes_its_action_results(db_session, website):
    decision = create_decision(
        db_session, website_id=website.id, final_decision=AUTOMATIC_APPROVAL_DENIED
    )
    create_action_result(
        db_session,
        decision_id=decision.id,
        website_id=website.id,
        action_type="approval",
        succeeded=True,
    )
    db_session.delete(decision)
    db_session.commit()
    assert db_session.query(AutoOnboardingActionResult).count() == 0
    assert db_session.query(AutoOnboardingDecision).count() == 0


# --- Audit actor -------------------------------------------------------------


def test_human_audit_entries_default_to_the_user_actor(db_session, make_user):
    user = make_user(email="actor@example.com")
    entry = record_audit(db_session, actor_id=user.id, action="configuration_approved")
    assert entry.actor_type == ACTOR_USER
    assert entry.actor_label is None
    assert entry.user_id == user.id


def test_a_system_audit_entry_is_distinguishable_from_a_human_one(db_session):
    entry = record_system_audit(
        db_session,
        action="website_automatically_approved",
        entity_type="website",
        entity_id=1,
        after={"policy_version": 1},
    )
    assert entry.actor_type == ACTOR_SYSTEM
    assert entry.actor_label == SYSTEM_ACTOR_LABEL
    # No user is credited: nobody approved this, and no submitter's
    # permissions authorized it.
    assert entry.user_id is None


def test_a_system_actor_has_no_user_row_and_therefore_no_login(db_session):
    from app.models.user import User

    record_system_audit(db_session, action="website_automatically_approved")
    assert (
        db_session.query(User).filter(User.email.ilike("%system%")).count() == 0
    ), "there must be no system account"
    assert db_session.query(User).count() == 0


def test_existing_audit_entries_keep_their_human_meaning(db_session, make_user):
    """The column defaults to `user`, so every historical entry still reads as
    a human action rather than becoming ambiguous."""
    user = make_user(email="legacy@example.com")
    db_session.add(AuditLog(user_id=user.id, action="website_created"))
    db_session.commit()
    entry = db_session.query(AuditLog).filter(AuditLog.action == "website_created").one()
    assert entry.actor_type == ACTOR_USER


# --- Nothing changed about onboarding yet ------------------------------------


def test_website_configuration_origin_defaults_to_unknown(db_session, make_city, make_website):
    city = make_city(name="Origin City", slug="origin-city")
    site = make_website(city, name="Origin Site", base_url="https://origin-site.example.org")
    assert site.configuration_origin is None
