"""Admin forms reject malformed or unknown ids with a 422, never a 500."""

from __future__ import annotations

import pytest

from app.core.permissions import REGISTERED_USER
from app.extraction.unsupported import UnsupportedReportData
from app.models.auto_onboarding_policy import AutoOnboardingPolicyCity, AutoOnboardingPolicyRole
from app.models.onboarding_batch import OnboardingBatch
from app.repositories.unsupported_site_report import create_unsupported_site_report
from app.services.auto_onboarding_policies import create_policy

POLICIES = "/admin/settings/onboarding-policies"
GOOD_URL = "https://example.com/events"


def _csrf(client) -> str:
    return client.cookies.get("csrf_token")


@pytest.fixture
def admin(client, make_super_admin, login):
    user = make_super_admin(email="bad-ids-root@example.com")
    login("bad-ids-root@example.com")
    return user


@pytest.fixture
def city(make_city):
    return make_city(name="Bad Id City", slug="bad-id-city")


@pytest.fixture
def policy(db_session):
    return create_policy(db_session, name="First policy", description=None, values={})


# --- onboarding submission ----------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [("city_id", "abc"), ("city_id", "999999"), ("policy_id", "abc"), ("policy_id", "999999")],
)
def test_onboard_submission_rejects_bad_ids(client, admin, city, db_session, field, value):
    data = {"urls": GOOD_URL, "city_id": str(city.id), "csrf_token": _csrf(client)}
    data[field] = value
    resp = client.post("/admin/websites/onboard", data=data, follow_redirects=False)
    assert resp.status_code == 422
    assert db_session.query(OnboardingBatch).count() == 0


# --- onboarding policies ------------------------------------------------------


@pytest.mark.parametrize("value", ["abc", "999999"])
@pytest.mark.parametrize("action", ["assign", "remove"])
def test_policy_city_assignment_rejects_bad_ids(client, admin, policy, db_session, value, action):
    resp = client.post(
        f"{POLICIES}/{policy.id}/cities",
        data={"city_id": value, "action": action, "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    # Removing an assignment that does not exist is a harmless no-op.
    expected = 303 if (action == "remove" and value.isdigit()) else 422
    assert resp.status_code == expected
    assert db_session.query(AutoOnboardingPolicyCity).count() == 0


@pytest.mark.parametrize("value", ["abc", "999999"])
def test_policy_roles_reject_bad_ids(client, admin, policy, db_session, value):
    resp = client.post(
        f"{POLICIES}/{policy.id}/roles",
        data={"role_ids": value, "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 422
    assert db_session.query(AutoOnboardingPolicyRole).count() == 0


def test_renaming_a_policy_to_an_existing_name_is_rejected(client, admin, policy, db_session):
    other = create_policy(db_session, name="Second policy", description=None, values={})
    resp = client.post(
        f"{POLICIES}/{other.id}",
        data={"name": "First policy", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 422
    db_session.refresh(other)
    assert other.name == "Second policy"

    # Saving a policy under its own name is still fine.
    resp = client.post(
        f"{POLICIES}/{other.id}",
        data={"name": "Second policy", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def test_policy_status_rejects_values_other_than_true_or_false(client, admin, policy, db_session):
    resp = client.post(
        f"{POLICIES}/{policy.id}/status",
        data={"active": "garbage", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 422
    db_session.refresh(policy)
    assert policy.active is True


# --- unsupported-site reports -------------------------------------------------


@pytest.fixture
def report(db_session, city, make_website):
    website = make_website(city, name="Bad Id Site")
    return create_unsupported_site_report(
        db_session,
        UnsupportedReportData(
            website_id=website.id,
            submitted_url=GOOD_URL,
            final_url=GOOD_URL,
            http_status=200,
            page_title="Some Site",
            detected_platform_evidence={},
            available_detector_results={},
            discovered_endpoints=[],
            browser_required=False,
            json_ld_presence=False,
            pagination_indicators={},
            access_denied_or_challenge_detected=False,
            failure_reason="no_pattern_matched",
            fingerprint="bad-ids-fp",
        ),
    )


def _assign(client, report, value):
    return client.post(
        f"/admin/unsupported-reports/{report.id}/assign",
        data={"assigned_user_id": value, "csrf_token": _csrf(client)},
        follow_redirects=False,
    )


@pytest.mark.parametrize("value", ["abc", "999999"])
def test_report_assignment_rejects_bad_ids(client, admin, report, db_session, value):
    assert _assign(client, report, value).status_code == 422
    db_session.refresh(report)
    assert report.assigned_user_id is None


def test_report_assignment_requires_reports_manage(client, admin, report, make_user, db_session):
    plain = make_user(email="no-reports@example.com", role_name=REGISTERED_USER)
    assert _assign(client, report, str(plain.id)).status_code == 422
    db_session.refresh(report)
    assert report.assigned_user_id is None

    assert _assign(client, report, str(admin.id)).status_code == 303
    db_session.refresh(report)
    assert report.assigned_user_id == admin.id
