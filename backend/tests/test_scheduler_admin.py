"""Phase 10: admin scheduler control endpoints."""

from __future__ import annotations

import pytest

from app.core.onboarding import ACTIVE
from app.services.scheduler import get_or_create_state

_CONFIG = {"configuration": {"pattern_name": "json_ld_event", "listing_url": "https://e.org/x"}}


@pytest.fixture
def admin_client(client, make_super_admin, login):
    make_super_admin(email="sched-root@example.com", password="root-pass-1234")
    login("sched-root@example.com", "root-pass-1234")
    return client


def _csrf_headers(client) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("csrf_token")}


@pytest.fixture
def eligible_website(make_city, make_website, db_session):
    city = make_city(is_active=True)
    website = make_website(city, approved_pattern=_CONFIG, is_active=True)
    website.onboarding_status = ACTIVE
    website.schedule_config = {"enabled": True, "interval_minutes": 60}
    db_session.commit()
    db_session.refresh(website)
    return website


def test_health_endpoint(admin_client):
    resp = admin_client.get("/admin/scheduler/health")
    assert resp.status_code == 200
    body = resp.json()
    assert "leader_is_fresh" in body
    assert "scheduled_count" in body


def test_run_now_schedules_an_eligible_site(admin_client, eligible_website, db_session):
    resp = admin_client.post(
        f"/admin/scheduler/websites/{eligible_website.id}/run-now",
        headers=_csrf_headers(admin_client),
    )
    assert resp.status_code == 200
    assert resp.json()["scheduled"] is True
    db_session.expire_all()
    assert get_or_create_state(db_session, eligible_website.id).next_run_at is not None


def test_run_now_refuses_an_ineligible_site(admin_client, make_city, make_website):
    city = make_city()
    website = make_website(city, name="Draft")  # not schedulable
    resp = admin_client.post(
        f"/admin/scheduler/websites/{website.id}/run-now",
        headers=_csrf_headers(admin_client),
    )
    assert resp.status_code == 409
    assert resp.json()["scheduled"] is False


def test_pause_and_resume(admin_client, eligible_website, db_session):
    pause = admin_client.post(
        f"/admin/scheduler/websites/{eligible_website.id}/pause",
        headers=_csrf_headers(admin_client),
    )
    assert pause.status_code == 200
    db_session.expire_all()
    assert get_or_create_state(db_session, eligible_website.id).paused is True

    resume = admin_client.post(
        f"/admin/scheduler/websites/{eligible_website.id}/resume",
        headers=_csrf_headers(admin_client),
    )
    assert resume.status_code == 200
    db_session.expire_all()
    assert get_or_create_state(db_session, eligible_website.id).paused is False


def test_state_changing_post_requires_csrf(admin_client, eligible_website):
    # No X-CSRF-Token header -> rejected.
    resp = admin_client.post(f"/admin/scheduler/websites/{eligible_website.id}/pause")
    assert resp.status_code in (400, 403)


def test_controls_require_permission(client, eligible_website):
    # Not logged in at all -> not authorized.
    resp = client.get("/admin/scheduler/health")
    assert resp.status_code in (401, 403)
