"""Restoring an archived website: an administrator-only action that brings the
website back inactive, with automatic imports off, and keeps everything else."""

import json
from datetime import UTC, datetime

from app.core.permissions import ADMINISTRATOR, EDITOR
from app.models.audit_log import AuditLog
from app.models.scheduler import SchedulerJobState
from app.services.scheduler import get_or_create_state
from app.services.websites import transition_website

APPROVED = {"pattern_name": "json_ld_event", "listing_url": "https://example.com/events"}
DRAFT = {"pattern_name": "ics_calendar", "listing_url": "https://example.com/events.ics"}


def _csrf(client) -> str:
    return client.cookies.get("csrf_token")


def _archived_website(db_session, make_city, make_website, *, approved=True, schedule=None):
    city = make_city(name="Restore City", slug="restore-city")
    website = make_website(
        city, name="Restore Site", approved_pattern=APPROVED if approved else None
    )
    website.onboarding_status = "active" if approved else "draft"
    website.is_active = approved
    website.configuration = DRAFT
    website.configuration_version = 3
    website.schedule_config = schedule
    db_session.commit()
    transition_website(db_session, website, "archived")
    return website


def _restore(client, website, *, follow=False):
    client.get(f"/admin/websites/{website.id}")
    return client.post(
        f"/admin/websites/{website.id}/restore",
        data={"csrf_token": _csrf(client)},
        follow_redirects=follow,
    )


def test_restore_brings_an_approved_website_back_inactive_and_unscheduled(
    client, make_super_admin, make_city, make_website, make_event, login, db_session
):
    make_super_admin(email="root@example.com")
    website = _archived_website(
        db_session, make_city, make_website,
        schedule={"enabled": True, "interval_minutes": 720},
    )
    event = make_event(website.city, title="Kept", website=website, is_active=False)
    archived_event = make_event(
        website.city, title="Old", canonical_url="https://example.com/old",
        website=website, archived=True,
    )
    website_id = website.id
    login("root@example.com")

    resp = _restore(client, website)
    assert resp.status_code == 303
    db_session.refresh(website)

    assert website.id == website_id
    assert website.onboarding_status == "inactive"
    assert website.is_active is False
    assert website.archived_at is None
    # Approved snapshot and the saved draft are untouched; nothing is approved.
    assert website.approved_pattern == APPROVED
    assert website.active_configuration_version == 1
    assert website.configuration == DRAFT
    assert website.configuration_version == 3
    # Automatic imports stay off, keeping the chosen interval.
    assert website.schedule_config == {"enabled": False, "interval_minutes": 720}
    state = db_session.query(SchedulerJobState).filter_by(website_id=website.id).one()
    assert state.paused is True
    # Events and their state are preserved.
    db_session.refresh(event)
    db_session.refresh(archived_event)
    assert event.website_id == website.id and event.is_active is False
    assert archived_event.archived_at is not None


def test_restore_without_approved_configuration_returns_to_draft(
    client, make_super_admin, make_city, make_website, login, db_session
):
    make_super_admin(email="root@example.com")
    website = _archived_website(db_session, make_city, make_website, approved=False)
    login("root@example.com")

    assert _restore(client, website).status_code == 303
    db_session.refresh(website)
    assert website.onboarding_status == "draft"
    assert website.approved_pattern is None
    assert website.configuration == DRAFT
    assert website.schedule_config == {"enabled": False, "interval_minutes": 1440}


def test_restored_website_stays_unscheduled_after_activation(
    client, make_super_admin, make_city, make_website, login, db_session
):
    """Activation gives a website with no schedule the default daily one; a
    restored website has a stored, disabled schedule, so that never happens."""
    make_super_admin(email="root@example.com")
    website = _archived_website(db_session, make_city, make_website)
    login("root@example.com")
    _restore(client, website)

    client.get(f"/admin/websites/{website.id}")
    resp = client.post(
        f"/admin/websites/{website.id}/status",
        data={"to_status": "active", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.refresh(website)
    assert website.onboarding_status == "active"
    assert website.schedule_config["enabled"] is False


def test_restore_does_not_run_an_import(
    client, make_super_admin, make_city, make_website, login, db_session, monkeypatch
):
    calls = []

    async def _no_run(*args, **kwargs):  # pragma: no cover - must not be called
        calls.append(args)

    monkeypatch.setattr("app.services.extraction_runs.run_extraction", _no_run)
    monkeypatch.setattr("app.routers.websites.run_extraction", _no_run)
    make_super_admin(email="root@example.com")
    website = _archived_website(db_session, make_city, make_website)
    state = get_or_create_state(db_session, website.id)
    state.next_run_at = datetime(2020, 1, 1, tzinfo=UTC)
    db_session.commit()
    login("root@example.com")

    _restore(client, website)
    assert calls == []
    db_session.refresh(website)
    assert website.last_success_at is None


def test_restore_is_audited(client, make_super_admin, make_city, make_website, login, db_session):
    make_super_admin(email="root@example.com")
    website = _archived_website(db_session, make_city, make_website)
    login("root@example.com")
    _restore(client, website)

    entry = (
        db_session.query(AuditLog)
        .filter_by(action="website_restored", entity_id=website.id)
        .one()
    )
    before, after = json.loads(entry.before_state), json.loads(entry.after_state)
    assert entry.user_id is not None
    assert before["onboarding_status"] == "archived"
    assert after["onboarding_status"] == "inactive"
    assert after["schedule_config"]["enabled"] is False


def test_administrator_can_restore(client, make_user, make_city, make_website, login, db_session):
    make_user(email="admin@example.com", role_name=ADMINISTRATOR)
    website = _archived_website(db_session, make_city, make_website)
    login("admin@example.com")
    assert _restore(client, website).status_code == 303
    db_session.refresh(website)
    assert website.onboarding_status == "inactive"


def test_editor_cannot_restore(client, make_user, make_city, make_website, login, db_session):
    make_user(email="editor@example.com", role_name=EDITOR)
    website = _archived_website(db_session, make_city, make_website)
    login("editor@example.com")

    assert _restore(client, website).status_code == 403
    db_session.refresh(website)
    assert website.onboarding_status == "archived"
    assert db_session.query(AuditLog).filter_by(action="website_restored").count() == 0


def test_restore_requires_csrf(
    client, make_super_admin, make_city, make_website, login, db_session
):
    make_super_admin(email="root@example.com")
    website = _archived_website(db_session, make_city, make_website)
    login("root@example.com")

    resp = client.post(
        f"/admin/websites/{website.id}/restore",
        data={"csrf_token": "wrong"},
        follow_redirects=False,
    )
    assert resp.status_code in (400, 403)
    db_session.refresh(website)
    assert website.onboarding_status == "archived"


def test_restore_refuses_a_website_that_is_not_archived(
    client, make_super_admin, make_city, make_website, login, db_session
):
    make_super_admin(email="root@example.com")
    city = make_city(name="Live City", slug="live-city")
    website = make_website(city, name="Live Site")
    login("root@example.com")

    assert _restore(client, website).status_code == 409
    db_session.refresh(website)
    assert website.onboarding_status == "draft"


def test_archived_website_page_offers_restore_only_to_administrators(
    client, make_user, make_city, make_website, login, db_session
):
    website = _archived_website(db_session, make_city, make_website)
    make_user(email="admin@example.com", role_name=ADMINISTRATOR)
    make_user(email="editor@example.com", role_name=EDITOR)

    login("admin@example.com")
    page = client.get(f"/admin/websites/{website.id}").text
    assert f"/admin/websites/{website.id}/restore" in page
    assert "Restore website" in page

    login("editor@example.com")
    page = client.get(f"/admin/websites/{website.id}").text
    assert f"/admin/websites/{website.id}/restore" not in page
