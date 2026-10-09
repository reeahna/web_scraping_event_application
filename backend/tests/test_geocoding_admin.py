"""Phase 11: admin geocoding endpoints."""

from __future__ import annotations

import pytest


@pytest.fixture
def admin_client(client, make_super_admin, login):
    make_super_admin(email="geo-root@example.com", password="root-pass-1234")
    login("geo-root@example.com", "root-pass-1234")
    return client


def _csrf(client) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("csrf_token")}


def test_status_reports_counts(admin_client, make_city, make_event):
    city = make_city()
    make_event(city, address="A", canonical_url="https://x/1")
    resp = admin_client.get("/admin/geocoding/status")
    assert resp.status_code == 200
    assert "counts" in resp.json()


def test_status_explains_skips_and_lists_unmatched_venues(admin_client, make_city, make_event):
    from datetime import date, timedelta

    soon = date.today() + timedelta(days=3)
    city = make_city(name="Bloomington", slug="bloomington")
    make_event(city, canonical_url="u1", start_date=soon,
               geocode_status="skipped", geocode_last_error="source_coordinates")
    make_event(city, canonical_url="u2", start_date=soon,
               geocode_status="skipped", geocode_last_error="no_address")
    for i in range(2):
        make_event(city, canonical_url=f"u3{i}", start_date=soon, venue="Buskirk",
                   geocode_status="needs_review", geocode_last_error="no_match")
    make_event(city, canonical_url="u4", start_date=date(2020, 1, 1), venue="Old Hall",
               geocode_status="needs_review", geocode_last_error="no_match")

    data = admin_client.get("/admin/geocoding/status").json()
    assert data["skipped_reasons"] == {"already_had_coordinates": 1, "no_location": 1}
    assert data["upcoming"]["needs_review"] == 2
    assert data["upcoming_by_city"]["Bloomington"]["needs_review"] == 2
    assert data["unmatched_upcoming"] == [
        {"city": "Bloomington", "venue": "Buskirk", "address": None, "events": 2}
    ]


def test_overview_page_is_readable(admin_client, make_city, make_event):
    from datetime import date, timedelta

    city = make_city(name="Bethlehem", slug="bethlehem")
    make_event(city, canonical_url="u1", start_date=date.today() + timedelta(days=2),
               venue="Williams Brew Stage", geocode_status="needs_review")
    resp = admin_client.get("/admin/geocoding")
    assert resp.status_code == 200
    assert "Map locations" in resp.text
    assert "No match found" in resp.text
    assert "Williams Brew Stage" in resp.text
    assert "/admin/events?q=Williams%20Brew%20Stage" in resp.text


def test_retry_requeues_a_failed_event(admin_client, make_city, make_event, db_session):
    city = make_city()
    event = make_event(city, address="A", geocode_status="failed", geocode_last_error="boom")
    resp = admin_client.post(
        f"/admin/geocoding/events/{event.id}/retry", headers=_csrf(admin_client)
    )
    assert resp.status_code == 200
    assert resp.json()["geocode_status"] == "pending"


def test_retry_requires_csrf(admin_client, make_city, make_event):
    city = make_city()
    event = make_event(city, address="A", geocode_status="failed")
    resp = admin_client.post(f"/admin/geocoding/events/{event.id}/retry")
    assert resp.status_code in (400, 403)


def test_status_requires_auth(client):
    resp = client.get("/admin/geocoding/status")
    assert resp.status_code in (401, 403)
