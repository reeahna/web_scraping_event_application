"""Admin routes for automatic-import scheduling and bulk manual import."""

from __future__ import annotations

import pytest

from app.core.onboarding import ACTIVE
from app.models.bulk_import import BulkImportRun
from app.services.scheduler import get_or_create_state

_CONFIG = {
    "configuration": {
        "pattern_name": "simpleview_events",
        "api_endpoint": "https://example.com/x/find/",
        "execution_strategy": "browser",
        "json_paths": {"events_root": "docs.docs"},
    }
}


@pytest.fixture
def admin_client(client, make_super_admin, login):
    make_super_admin(email="imp-root@example.com", password="root-pass-1234")
    login("imp-root@example.com", "root-pass-1234")
    return client


def _csrf(client) -> str:
    return client.cookies.get("csrf_token")


@pytest.fixture
def eligible_website(make_city, make_website, db_session):
    city = make_city(is_active=True)
    website = make_website(city, name="Visit Example", approved_pattern=_CONFIG, is_active=True)
    website.onboarding_status = ACTIVE
    website.schedule_config = {"enabled": True, "interval_minutes": 60}
    db_session.commit()
    db_session.refresh(website)
    return website


# --- operations page ---------------------------------------------------------


def test_operations_page_renders(admin_client, eligible_website):
    resp = admin_client.get("/admin/scheduler")
    assert resp.status_code == 200
    assert "Import all active websites" in resp.text
    assert "Scheduler process" in resp.text
    assert "python -m app.scheduler" in resp.text


# --- bulk import route -------------------------------------------------------


def test_bulk_import_requires_csrf(admin_client, eligible_website):
    resp = admin_client.post("/admin/scheduler/bulk-import", data={"csrf_token": "wrong"})
    assert resp.status_code in (400, 403)


def test_bulk_import_requires_authentication(client):
    resp = client.post(
        "/admin/scheduler/bulk-import", data={"csrf_token": "x"}, follow_redirects=False
    )
    assert resp.status_code in (302, 303, 401, 403)


def test_bulk_import_creates_queued_run(admin_client, eligible_website, db_session):
    resp = admin_client.post(
        "/admin/scheduler/bulk-import",
        data={"csrf_token": _csrf(admin_client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    run = db_session.query(BulkImportRun).one()
    assert run.status == "queued"
    assert run.eligible_count == 1
    assert run.browser_count == 1
    assert f"/admin/scheduler/bulk-imports/{run.id}" in resp.headers["location"]


def test_bulk_import_double_submit_guarded(admin_client, eligible_website, db_session):
    token = _csrf(admin_client)
    admin_client.post(
        "/admin/scheduler/bulk-import", data={"csrf_token": token}, follow_redirects=False
    )
    resp = admin_client.post(
        "/admin/scheduler/bulk-import", data={"csrf_token": token}, follow_redirects=False
    )
    # No second run is created while one is still unfinished.
    assert db_session.query(BulkImportRun).count() == 1
    assert resp.status_code == 303


def test_bulk_detail_page_renders(admin_client, eligible_website, db_session):
    admin_client.post(
        "/admin/scheduler/bulk-import", data={"csrf_token": _csrf(admin_client)},
        follow_redirects=False,
    )
    run = db_session.query(BulkImportRun).one()
    resp = admin_client.get(f"/admin/scheduler/bulk-imports/{run.id}")
    assert resp.status_code == 200
    assert "Per-website results" in resp.text
    assert "Visit Example" in resp.text


# --- website detail: automatic imports section -------------------------------


def test_website_detail_shows_automatic_imports(admin_client, eligible_website):
    resp = admin_client.get(f"/admin/websites/{eligible_website.id}")
    assert resp.status_code == 200
    assert "Automatic imports" in resp.text
    assert "Every hour" in resp.text  # interval_minutes = 60
    assert "Run event import now" in resp.text
    assert "Queue scheduled run now" in resp.text
    assert "Browser" in resp.text


# --- schedule edit -----------------------------------------------------------


def test_schedule_edit_updates_interval(admin_client, eligible_website, db_session):
    resp = admin_client.post(
        f"/admin/websites/{eligible_website.id}/schedule",
        data={
            "csrf_token": _csrf(admin_client),
            "schedule_enabled": "on",
            "interval_minutes": "360",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.expire_all()
    db_session.refresh(eligible_website)
    assert eligible_website.schedule_config["interval_minutes"] == 360
    assert eligible_website.schedule_config["enabled"] is True


def test_schedule_edit_disable(admin_client, eligible_website, db_session):
    resp = admin_client.post(
        f"/admin/websites/{eligible_website.id}/schedule",
        data={"csrf_token": _csrf(admin_client), "interval_minutes": "1440"},  # checkbox unchecked
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.expire_all()
    db_session.refresh(eligible_website)
    assert eligible_website.schedule_config["enabled"] is False
    assert get_or_create_state(db_session, eligible_website.id).paused is True


def test_schedule_edit_enforces_minimum(admin_client, eligible_website, db_session):
    admin_client.post(
        f"/admin/websites/{eligible_website.id}/schedule",
        data={"csrf_token": _csrf(admin_client), "schedule_enabled": "on", "interval_minutes": "5"},
        follow_redirects=False,
    )
    db_session.expire_all()
    db_session.refresh(eligible_website)
    assert eligible_website.schedule_config["interval_minutes"] == 15  # clamped up to the minimum


# --- queue scheduled run now (distinct from immediate import) ----------------


def test_queue_now_sets_next_run_to_now(admin_client, eligible_website, db_session):
    from datetime import UTC, datetime

    resp = admin_client.post(
        f"/admin/scheduler/websites/{eligible_website.id}/queue-now",
        data={"csrf_token": _csrf(admin_client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.expire_all()
    state = get_or_create_state(db_session, eligible_website.id)
    assert state.next_run_at is not None
    stored = state.next_run_at
    stored = stored.replace(tzinfo=UTC) if stored.tzinfo is None else stored
    assert (datetime.now(UTC) - stored).total_seconds() < 60
