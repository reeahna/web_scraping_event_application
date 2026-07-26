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
