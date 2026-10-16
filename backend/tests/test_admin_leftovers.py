"""Regression tests for admin leftovers: missing audit records, duplicate-status
and location-correction validation, and odd private-address URL spellings."""

from __future__ import annotations

import pytest

from app.core.onboarding import ACTIVE
from app.core.url_safety import UnsafeURLError, validate_public_url
from app.models.audit_log import AuditLog
from app.models.event import Event
from app.services.scheduler import get_or_create_state

_CONFIG = {"configuration": {"pattern_name": "json_ld_event", "listing_url": "https://e.org/x"}}


def _csrf(client) -> str:
    return client.cookies.get("csrf_token")


@pytest.fixture
def admin(client, make_super_admin, login):
    user = make_super_admin(email="leftovers-root@example.com")
    login("leftovers-root@example.com")
    return user


@pytest.fixture
def eligible_website(make_city, make_website, db_session):
    city = make_city(is_active=True)
    website = make_website(city, approved_pattern=_CONFIG, is_active=True)
    website.onboarding_status = ACTIVE
    website.schedule_config = {"enabled": True, "interval_minutes": 60}
    db_session.commit()
    db_session.refresh(website)
    return website


def _audits(db_session, action: str) -> list[AuditLog]:
    db_session.expire_all()
    return db_session.query(AuditLog).filter(AuditLog.action == action).all()


# --- 1. audit records ---------------------------------------------------------


def test_recategorize_writes_audit_record(client, admin, make_city, make_event, db_session):
    event = make_event(make_city())
    response = client.post(
        f"/admin/events/{event.id}/categorize",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    entries = _audits(db_session, "event_recategorized")
    assert len(entries) == 1
    assert entries[0].entity_type == "event"
    assert entries[0].entity_id == event.id
    assert entries[0].user_id == admin.id


@pytest.mark.parametrize(
    ("path", "action"),
    [
        ("run-now", "scheduler_run_queued"),
        ("pause", "scheduler_paused"),
        ("resume", "scheduler_resumed"),
    ],
)
def test_scheduler_json_actions_write_audit_records(
    client, admin, eligible_website, db_session, path, action
):
    response = client.post(
        f"/admin/scheduler/websites/{eligible_website.id}/{path}",
        headers={"X-CSRF-Token": _csrf(client)},
    )
    assert response.status_code == 200
    entries = _audits(db_session, action)
    assert len(entries) == 1
    assert entries[0].entity_type == "website"
    assert entries[0].entity_id == eligible_website.id
    assert entries[0].user_id == admin.id


def test_scheduler_cancel_writes_audit_record(client, admin, eligible_website, db_session):
    get_or_create_state(db_session, eligible_website.id)
    db_session.commit()
    response = client.post(
        f"/admin/scheduler/websites/{eligible_website.id}/cancel",
        headers={"X-CSRF-Token": _csrf(client)},
    )
    assert response.status_code == 200
    assert response.json()["cancel_requested"] is True
    entries = _audits(db_session, "scheduler_cancel_requested")
    assert len(entries) == 1
    assert entries[0].entity_id == eligible_website.id


def test_scheduler_queue_now_writes_audit_record(client, admin, eligible_website, db_session):
    response = client.post(
        f"/admin/scheduler/websites/{eligible_website.id}/queue-now",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    entries = _audits(db_session, "scheduler_run_queued")
    assert len(entries) == 1
    assert entries[0].entity_id == eligible_website.id


def test_refused_run_now_writes_no_audit_record(
    client, admin, make_city, make_website, db_session
):
    website = make_website(make_city(), name="Draft")
    response = client.post(
        f"/admin/scheduler/websites/{website.id}/run-now",
        headers={"X-CSRF-Token": _csrf(client)},
    )
    assert response.status_code == 409
    assert _audits(db_session, "scheduler_run_queued") == []


# --- 2. duplicate status ------------------------------------------------------


def _set_duplicate(client, event_id: int, status: str, preferred: int | str = ""):
    return client.post(
        f"/admin/events/{event_id}/duplicate-status",
        data={
            "duplicate_status": status,
            "preferred_event_id": preferred,
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )


def test_confirmed_duplicate_requires_preferred_event(
    client, admin, make_city, make_event, db_session
):
    event = make_event(make_city())
    response = _set_duplicate(client, event.id, "confirmed_duplicate")
    assert response.status_code == 422
    db_session.expire_all()
    assert db_session.get(Event, event.id).duplicate_status == "not_reviewed"


def test_event_cannot_be_its_own_preferred_duplicate(
    client, admin, make_city, make_event
):
    event = make_event(make_city())
    assert _set_duplicate(client, event.id, "confirmed_duplicate", event.id).status_code == 422


def test_duplicate_cycles_are_rejected(client, admin, make_city, make_event, db_session):
    city = make_city()
    a = make_event(city, title="A", canonical_url="https://example.com/a")
    b = make_event(city, title="B", canonical_url="https://example.com/b")
    c = make_event(city, title="C", canonical_url="https://example.com/c")

    assert _set_duplicate(client, a.id, "confirmed_duplicate", b.id).status_code == 303
    # B -> A would close the loop A -> B -> A.
    assert _set_duplicate(client, b.id, "confirmed_duplicate", a.id).status_code == 422
    # A longer loop: B -> C, then C -> A closes A -> B -> C -> A.
    assert _set_duplicate(client, b.id, "confirmed_duplicate", c.id).status_code == 303
    assert _set_duplicate(client, c.id, "confirmed_duplicate", a.id).status_code == 422
    db_session.expire_all()
    assert db_session.get(Event, c.id).duplicate_preferred_event_id is None
    # Non-confirmed statuses carry no pointer and stay allowed.
    assert _set_duplicate(client, c.id, "not_duplicate").status_code == 303


# --- 3. location correction ---------------------------------------------------


@pytest.mark.parametrize(("latitude", "longitude"), [("39.17", ""), ("", "-86.52")])
def test_location_correction_requires_both_coordinates(
    client, admin, make_city, make_event, db_session, latitude, longitude
):
    event = make_event(make_city())
    response = client.post(
        f"/admin/events/{event.id}/location-correction",
        data={
            "venue": "Hall",
            "address": "",
            "latitude": latitude,
            "longitude": longitude,
            "csrf_token": _csrf(client),
        },
    )
    assert response.status_code == 422
    db_session.expire_all()
    stored = db_session.get(Event, event.id)
    assert stored.corrected_latitude is None
    assert stored.corrected_longitude is None
    assert stored.corrected_venue is None


def test_location_correction_without_coordinates_still_allowed(
    client, admin, make_city, make_event
):
    event = make_event(make_city())
    response = client.post(
        f"/admin/events/{event.id}/location-correction",
        data={
            "venue": "Hall",
            "address": "",
            "latitude": "",
            "longitude": "",
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303


# --- 4. URL safety ------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.1/",
        "http://0x7f000001/",
        "http://0177.0.0.1/",
        "http://10.1/",
        "http://localhost./",
        "http://foo.localhost./",
        "http://example.com:99999/",
    ],
)
def test_odd_private_address_spellings_are_rejected(url):
    with pytest.raises(UnsafeURLError):
        validate_public_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/events",
        "https://example.com:8443/events",
        "http://8.8.8.8/",
        "https://example.com./events",
    ],
)
def test_ordinary_public_urls_still_pass(url):
    assert validate_public_url(url) == url
