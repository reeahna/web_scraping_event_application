"""Increment 5: policy administration, confirmations, and explanation UI."""

from __future__ import annotations

import pytest

from app.core.exceptions import AppError
from app.core.permissions import EDITOR, REGISTERED_USER
from app.models.auto_onboarding_policy import AutoOnboardingPolicy, AutoOnboardingPolicyCity
from app.repositories.auto_onboarding import global_default_policy, resolve_policy
from app.services.auto_onboarding_policies import (
    ACTIVATION_CONFIRMATION_PHRASE,
    APPROVAL_CONFIRMATION_PHRASE,
    assign_city,
    create_policy,
    set_active,
    set_global_default,
    set_roles,
    update_policy,
)

BASE_URL = "/admin/settings/onboarding-policies"


def _csrf(client) -> str:
    return client.cookies.get("csrf_token")


@pytest.fixture
def admin_client(client, make_super_admin, login):
    make_super_admin(email="policy-root@example.com", password="root-pass-1234")
    login("policy-root@example.com", "root-pass-1234")
    return client


@pytest.fixture
def policy(db_session):
    return create_policy(
        db_session,
        name="Structured sources",
        description="Allows JSON-LD only",
        values={"allowed_pattern_names": ["json_ld_event"]},
    )


# --- service-level rules ------------------------------------------------------


def test_creating_a_policy_starts_at_version_one(db_session, policy):
    assert policy.version == 1
    assert policy.allowed_pattern_names == ["json_ld_event"]
    assert policy.automatic_approval_enabled is False


def test_an_unregistered_pattern_is_rejected(db_session):
    with pytest.raises(AppError):
        create_policy(
            db_session, name="Bad patterns", description=None,
            values={"allowed_pattern_names": ["not_a_pattern"]},
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("minimum_valid_percentage", 1.5),
        ("minimum_valid_percentage", -0.1),
        ("minimum_detector_confidence", 42),
        ("minimum_valid_events", -1),
    ],
)
def test_out_of_range_thresholds_are_rejected(db_session, field, value):
    with pytest.raises(AppError):
        create_policy(db_session, name=f"Bad {field}", description=None, values={field: value})


def test_a_meaningful_change_bumps_the_version(db_session, policy):
    change = update_policy(
        db_session, policy, values={"minimum_valid_events": 9}
    )
    assert change.version_bumped is True
    assert policy.version == 2
    assert "minimum_valid_events" in change.changed_fields


def test_renaming_a_policy_does_not_bump_the_version(db_session, policy):
    change = update_policy(
        db_session, policy, name="Renamed", values={}
    )
    assert change.version_bumped is False
    assert policy.version == 1
    assert policy.name == "Renamed"


def test_enabling_automatic_approval_requires_the_confirmation_phrase(db_session, policy):
    with pytest.raises(AppError) as exc:
        update_policy(db_session, policy, values={"automatic_approval_enabled": True})
    assert APPROVAL_CONFIRMATION_PHRASE in str(exc.value)

    update_policy(
        db_session,
        policy,
        values={"automatic_approval_enabled": True},
        confirmations={"approval": APPROVAL_CONFIRMATION_PHRASE},
    )
    assert policy.automatic_approval_enabled is True


def test_enabling_automatic_activation_requires_its_own_stronger_phrase(db_session, policy):
    # The approval phrase alone is not enough for activation.
    with pytest.raises(AppError):
        update_policy(
            db_session,
            policy,
            values={"automatic_approval_enabled": True, "automatic_activation_enabled": True},
            confirmations={"approval": APPROVAL_CONFIRMATION_PHRASE},
        )
    update_policy(
        db_session,
        policy,
        values={"automatic_approval_enabled": True, "automatic_activation_enabled": True},
        confirmations={
            "approval": APPROVAL_CONFIRMATION_PHRASE,
            "activation": ACTIVATION_CONFIRMATION_PHRASE,
        },
    )
    assert policy.automatic_activation_enabled is True


def test_disabling_automatic_approval_needs_no_confirmation(db_session, policy):
    update_policy(
        db_session,
        policy,
        values={"automatic_approval_enabled": True},
        confirmations={"approval": APPROVAL_CONFIRMATION_PHRASE},
    )
    update_policy(db_session, policy, values={"automatic_approval_enabled": False})
    assert policy.automatic_approval_enabled is False


def test_promoting_a_global_default_demotes_the_incumbent(db_session, policy):
    incumbent = global_default_policy(db_session)
    assert incumbent.id != policy.id
    set_global_default(db_session, policy)
    db_session.refresh(incumbent)

    assert policy.is_global_default is True
    assert incumbent.is_global_default is False
    assert global_default_policy(db_session).id == policy.id


def test_a_deactivated_policy_cannot_become_the_global_default(db_session, policy):
    set_active(db_session, policy, active=False)
    with pytest.raises(AppError):
        set_global_default(db_session, policy)


def test_a_city_cannot_be_assigned_to_two_policies(db_session, policy, make_city):
    city = make_city(name="Assign City", slug="assign-city")
    other = create_policy(db_session, name="Other policy", description=None, values={})
    assign_city(db_session, policy, city_id=city.id)
    with pytest.raises(AppError):
        assign_city(db_session, other, city_id=city.id)


def test_assigning_an_unknown_city_is_rejected(db_session, policy):
    with pytest.raises(AppError):
        assign_city(db_session, policy, city_id=99999)


def test_an_unknown_role_is_rejected(db_session, policy):
    with pytest.raises(AppError):
        set_roles(db_session, policy, role_ids=[99999])


def test_precedence_is_city_then_global(db_session, policy, make_city):
    city = make_city(name="Precedence City", slug="precedence-city")
    assert resolve_policy(db_session, city_id=city.id).is_global_default is True
    assign_city(db_session, policy, city_id=city.id)
    assert resolve_policy(db_session, city_id=city.id).id == policy.id


# --- routes -------------------------------------------------------------------


def test_the_policy_list_and_forms_load(admin_client, policy):
    listing = admin_client.get(BASE_URL)
    assert listing.status_code == 200
    assert "Structured sources" in listing.text

    new_form = admin_client.get(f"{BASE_URL}/new")
    assert new_form.status_code == 200
    assert APPROVAL_CONFIRMATION_PHRASE in new_form.text
    assert ACTIVATION_CONFIRMATION_PHRASE in new_form.text
    # Structured controls only — no raw JSON anywhere on the main path.
    assert "raw_json" not in new_form.text
    assert 'name="allowed_pattern_names"' in new_form.text
    assert 'name="minimum_valid_percentage"' in new_form.text

    detail = admin_client.get(f"{BASE_URL}/{policy.id}")
    assert detail.status_code == 200
    edit = admin_client.get(f"{BASE_URL}/{policy.id}/edit")
    assert edit.status_code == 200


def test_creating_a_policy_through_the_form(admin_client, db_session):
    resp = admin_client.post(
        BASE_URL,
        data={
            "csrf_token": _csrf(admin_client),
            "name": "Form policy",
            "description": "created via the form",
            "allowed_pattern_names": ["json_ld_event", "the_events_calendar"],
            "active": "on",
            "minimum_valid_events": "4",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    created = (
        db_session.query(AutoOnboardingPolicy)
        .filter(AutoOnboardingPolicy.name == "Form policy")
        .one()
    )
    assert created.allowed_pattern_names == ["json_ld_event", "the_events_calendar"]
    assert created.minimum_valid_events == 4
    assert created.automatic_approval_enabled is False


def test_enabling_approval_through_the_form_requires_the_phrase(
    admin_client, db_session, policy
):
    resp = admin_client.post(
        f"{BASE_URL}/{policy.id}",
        data={
            "csrf_token": _csrf(admin_client),
            "name": policy.name,
            "automatic_approval_enabled": "on",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 422
    db_session.refresh(policy)
    assert policy.automatic_approval_enabled is False

    ok = admin_client.post(
        f"{BASE_URL}/{policy.id}",
        data={
            "csrf_token": _csrf(admin_client),
            "name": policy.name,
            "automatic_approval_enabled": "on",
            "confirm_approval": APPROVAL_CONFIRMATION_PHRASE,
        },
        follow_redirects=False,
    )
    assert ok.status_code == 303
    db_session.refresh(policy)
    assert policy.automatic_approval_enabled is True


def test_unexpected_form_fields_are_rejected(admin_client, policy):
    resp = admin_client.post(
        f"{BASE_URL}/{policy.id}",
        data={
            "csrf_token": _csrf(admin_client),
            "name": policy.name,
            "is_global_default": "on",  # not an editable form field
        },
        follow_redirects=False,
    )
    assert resp.status_code == 422


def test_policy_routes_enforce_csrf(admin_client, policy):
    resp = admin_client.post(
        f"{BASE_URL}/{policy.id}",
        data={"csrf_token": "wrong", "name": policy.name},
        follow_redirects=False,
    )
    assert resp.status_code == 403


@pytest.mark.parametrize("role", [REGISTERED_USER, EDITOR])
def test_policy_administration_requires_settings_manage(client, make_user, login, role):
    """An Editor can submit onboarding batches but must not be able to edit
    the policy that decides what gets approved."""
    email = f"{role.replace(' ', '')}@example.com"
    make_user(email=email, password="user-pass-1234", role_name=role)
    login(email, "user-pass-1234")
    resp = client.get(BASE_URL, follow_redirects=False)
    assert resp.status_code in (302, 303, 403)


def test_assigning_a_city_through_the_form(admin_client, db_session, policy, make_city):
    city = make_city(name="Route City", slug="route-city")
    resp = admin_client.post(
        f"{BASE_URL}/{policy.id}/cities",
        data={"csrf_token": _csrf(admin_client), "city_id": str(city.id), "action": "assign"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert (
        db_session.query(AutoOnboardingPolicyCity)
        .filter(AutoOnboardingPolicyCity.city_id == city.id)
        .count()
        == 1
    )


# --- decision explanation UI ---------------------------------------------------


def test_the_decision_pages_render(admin_client, db_session, policy, make_city, make_website):
    from app.services.auto_onboarding_decision import AutoOnboardingDecisionService, DecisionContext
    from app.services.auto_onboarding_persistence import record_decision

    city = make_city(name="Explain City", slug="explain-city")
    website = make_website(city, name="Explain Site", base_url="https://explain.example.org")
    result = AutoOnboardingDecisionService().evaluate(
        DecisionContext(
            policy=None,
            website_id=website.id,
            website_is_archived=False,
            website_onboarding_status="detected",
            city_id=city.id,
            city_is_active=True,
        )
    )
    decision = record_decision(db_session, result, website=website)

    detail = admin_client.get(f"/admin/onboarding/decisions/{decision.id}")
    assert detail.status_code == 200
    assert "no automatic-onboarding policy applies" in detail.text
    assert "Rules that failed" in detail.text
    assert "Manual approval remains available" in detail.text

    history = admin_client.get(f"/admin/websites/{website.id}/decisions")
    assert history.status_code == 200
    assert "append-only" in history.text
    assert f"/admin/onboarding/decisions/{decision.id}" in history.text


def test_decision_history_requires_view_permission(client, make_user, login):
    make_user(email="nobody@example.com", password="user-pass-1234", role_name=REGISTERED_USER)
    login("nobody@example.com", "user-pass-1234")
    resp = client.get("/admin/websites/1/decisions", follow_redirects=False)
    assert resp.status_code in (302, 303, 403)
