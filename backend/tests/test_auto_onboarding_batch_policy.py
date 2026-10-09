"""Phase 8D completion: batch-level policy override and success notifications.

A batch may name a policy explicitly. That selection is an *input* to
evaluation, never an authorization token: choosing a permissive policy grants
the submitter nothing the policy's own rules don't already permit, and
choosing one that enables approval/activation requires settings.manage.
"""

from __future__ import annotations

import asyncio
import itertools

import httpx
import pytest

from app.core.permissions import REGISTERED_USER
from app.models.auto_onboarding_policy import AutoOnboardingPolicy
from app.models.notification import Notification
from app.models.onboarding_batch import OnboardingBatch
from app.models.website import Website
from app.repositories.auto_onboarding import (
    latest_decision_for_website,
    resolve_policy,
)
from app.services.bulk_onboarding import create_batch_from_submission, process_batch
from app.services.onboarding_submission import SubmissionLimits, parse_url_lines
from tests.extraction_helpers import load_fixture, patched_http_fetch

LISTING_URL = "https://venue.example.org/events"
LIMITS = SubmissionLimits(max_urls=50, max_csv_rows=50, max_csv_bytes=100_000, max_url_length=2000)
_names = itertools.count(1)


@pytest.fixture
def listing_body():
    return load_fixture("jsonld_multiple_events.html")


@pytest.fixture
def city(make_city):
    return make_city(name="Batch City", slug="batch-city", timezone="UTC")


@pytest.fixture
def make_policy(db_session):
    def _make(**overrides):
        policy = AutoOnboardingPolicy(
            name=f"Batch policy {next(_names)}",
            active=True,
            automatic_approval_enabled=True,
            allowed_pattern_names=["json_ld_event"],
            minimum_events_found=1,
            minimum_valid_events=1,
            minimum_distinct_events=1,
        )
        db_session.add(policy)
        db_session.commit()
        db_session.refresh(policy)
        for key, value in overrides.items():
            setattr(policy, key, value)
        db_session.commit()
        db_session.refresh(policy)
        return policy

    return _make


def _handler(body: str):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) != LISTING_URL:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    return handler


# --- resolution precedence ----------------------------------------------------


def test_a_selected_policy_takes_precedence_over_city_and_global(db_session, city, make_policy):
    selected = make_policy()
    assert resolve_policy(db_session, city_id=city.id, selected_policy_id=selected.id).id == (
        selected.id
    )


def test_a_selected_inactive_policy_resolves_to_none(db_session, city, make_policy):
    selected = make_policy(active=False)
    assert resolve_policy(db_session, city_id=city.id, selected_policy_id=selected.id) is None


# --- end to end ---------------------------------------------------------------


def test_a_batch_selected_policy_drives_the_outcome(db_session, city, make_policy, listing_body):
    selected = make_policy()
    parsed = parse_url_lines(LISTING_URL, LIMITS)
    batch = create_batch_from_submission(
        db_session,
        parsed,
        submitted_by_user_id=None,
        default_city_id=city.id,
        default_timezone=None,
        redetect_existing=False,
        source_kind="single",
        selected_policy_id=selected.id,
        correlation_id="batch-policy-test",
    )
    assert batch.selected_policy_id == selected.id

    with patched_http_fetch(_handler(listing_body)):
        asyncio.run(process_batch(db_session, batch, limit=5))
    db_session.refresh(batch)

    job = batch.jobs[0]
    assert job.status == "automatically_approved"
    decision = latest_decision_for_website(db_session, job.website_id)
    assert decision.policy_id == selected.id


def test_the_global_default_denies_when_no_policy_is_selected(db_session, city, listing_body):
    parsed = parse_url_lines(LISTING_URL, LIMITS)
    batch = create_batch_from_submission(
        db_session,
        parsed,
        submitted_by_user_id=None,
        default_city_id=city.id,
        default_timezone=None,
        redetect_existing=False,
        source_kind="single",
        correlation_id="no-policy-test",
    )
    with patched_http_fetch(_handler(listing_body)):
        asyncio.run(process_batch(db_session, batch, limit=5))
    db_session.refresh(batch)

    website = db_session.get(Website, batch.jobs[0].website_id)
    assert website.approved_pattern is None


# --- authorization at submission time -----------------------------------------


def _csrf(client) -> str:
    return client.cookies.get("csrf_token")


def test_a_submit_only_user_cannot_select_an_approving_policy(
    client, db_session, city, make_policy, make_user, login
):
    """The backend blocks it even if the field is forged — hidden controls are
    not the security boundary."""
    make_policy()  # an approving policy exists
    # Editor has sites.create but not settings.manage.
    from app.core.permissions import EDITOR

    make_user(email="editor@example.com", password="user-pass-1234", role_name=EDITOR)
    login("editor@example.com", "user-pass-1234")
    approving = db_session.query(AutoOnboardingPolicy).filter(
        AutoOnboardingPolicy.automatic_approval_enabled.is_(True)
    ).first()

    resp = client.post(
        "/admin/websites/onboard",
        data={
            "urls": LISTING_URL,
            "city_id": str(city.id),
            "policy_id": str(approving.id),
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 422
    assert "settings.manage" in resp.text
    assert db_session.query(OnboardingBatch).count() == 0


def test_a_settings_manager_may_select_an_approving_policy(
    client, db_session, city, make_policy, make_super_admin, login, listing_body
):
    approving = make_policy()
    make_super_admin(email="manager@example.com", password="root-pass-1234")
    login("manager@example.com", "root-pass-1234")

    with patched_http_fetch(_handler(listing_body)):
        resp = client.post(
            "/admin/websites/onboard",
            data={
                "urls": LISTING_URL,
                "city_id": str(city.id),
                "policy_id": str(approving.id),
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
    assert resp.status_code == 303
    batch = db_session.query(OnboardingBatch).one()
    assert batch.selected_policy_id == approving.id


def test_the_policy_selector_is_only_shown_to_settings_managers(
    client, make_user, make_super_admin, login, city
):
    from app.core.permissions import EDITOR

    make_user(email="plain-editor@example.com", password="user-pass-1234", role_name=EDITOR)
    login("plain-editor@example.com", "user-pass-1234")
    editor_view = client.get("/admin/websites/onboard").text
    assert 'name="policy_id"' not in editor_view

    make_super_admin(email="manager2@example.com", password="root-pass-1234")
    login("manager2@example.com", "root-pass-1234")
    manager_view = client.get("/admin/websites/onboard").text
    assert 'name="policy_id"' in manager_view


def test_submit_permission_is_still_required(client, make_user, login):
    make_user(email="nobody@example.com", password="user-pass-1234", role_name=REGISTERED_USER)
    login("nobody@example.com", "user-pass-1234")
    resp = client.get("/admin/websites/onboard", follow_redirects=False)
    assert resp.status_code in (302, 303, 403)


# --- success notifications ----------------------------------------------------


def test_a_batch_that_auto_approves_notifies_once(
    db_session, city, make_policy, make_super_admin, listing_body
):
    make_super_admin(email="approve-recipient@example.com")  # a recipient must exist
    selected = make_policy()
    parsed = parse_url_lines(LISTING_URL, LIMITS)
    batch = create_batch_from_submission(
        db_session,
        parsed,
        submitted_by_user_id=None,
        default_city_id=city.id,
        default_timezone=None,
        redetect_existing=False,
        source_kind="single",
        selected_policy_id=selected.id,
        correlation_id="notify-test",
    )
    with patched_http_fetch(_handler(listing_body)):
        asyncio.run(process_batch(db_session, batch, limit=5))

    types = [n.notification_type for n in db_session.query(Notification).all()]
    assert types.count("onboarding_batch_auto_approved") == 1
    assert "onboarding_batch_auto_activated" not in types  # activation was disabled


def test_a_batch_that_auto_activates_notifies(
    db_session, city, make_policy, make_super_admin, listing_body
):
    make_super_admin(email="activate-recipient@example.com")
    selected = make_policy(automatic_activation_enabled=True)
    parsed = parse_url_lines(LISTING_URL, LIMITS)
    batch = create_batch_from_submission(
        db_session,
        parsed,
        submitted_by_user_id=None,
        default_city_id=city.id,
        default_timezone=None,
        redetect_existing=False,
        source_kind="single",
        selected_policy_id=selected.id,
        correlation_id="notify-activate-test",
    )
    with patched_http_fetch(_handler(listing_body)):
        asyncio.run(process_batch(db_session, batch, limit=5))

    types = [n.notification_type for n in db_session.query(Notification).all()]
    assert "onboarding_batch_auto_activated" in types
