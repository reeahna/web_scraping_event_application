"""Admin access key: an automated job can use the admin pages with
"Authorization: Bearer <key>" instead of a browser sign-in."""

from __future__ import annotations

import pytest

from app.config import get_settings

KEY = "k" * 40


@pytest.fixture
def key_settings(monkeypatch, make_super_admin):
    make_super_admin(email="robot@example.com")
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_api_key", KEY)
    monkeypatch.setattr(settings, "admin_api_key_user_email", "Robot@Example.com")
    return settings


def _get(client, key: str):
    return client.get(
        "/admin/scheduler/health",
        headers={"Authorization": f"Bearer {key}"},
        follow_redirects=False,
    )


def test_the_right_key_opens_admin_pages(client, key_settings):
    resp = _get(client, KEY)
    assert resp.status_code == 200
    assert "leader_is_fresh" in resp.json()


def test_a_wrong_key_is_refused(client, key_settings):
    assert _get(client, "x" * 40).status_code != 200


def test_without_configuration_a_key_is_refused(client, make_super_admin):
    make_super_admin(email="robot@example.com")
    assert _get(client, KEY).status_code != 200


def test_a_short_configured_key_is_ignored(client, key_settings, monkeypatch):
    monkeypatch.setattr(key_settings, "admin_api_key", "short")
    assert _get(client, "short").status_code != 200


def test_a_disabled_account_is_refused(client, key_settings, db_session):
    from app.models.user import User

    user = db_session.query(User).filter(User.email == "robot@example.com").one()
    user.is_active = False
    db_session.commit()
    assert _get(client, KEY).status_code != 200


def test_the_account_needs_the_permission(client, make_user, monkeypatch):
    make_user(email="plain@example.com")
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_api_key", KEY)
    monkeypatch.setattr(settings, "admin_api_key_user_email", "plain@example.com")
    assert _get(client, KEY).status_code == 403


def test_forms_still_need_the_csrf_token(client, key_settings, make_city, make_website):
    city = make_city(is_active=True)
    website = make_website(city)
    auth = {"Authorization": f"Bearer {KEY}"}
    url = f"/admin/scheduler/websites/{website.id}/queue-now"

    refused = client.post(url, headers=auth, data={"csrf_token": "guess"},
                          follow_redirects=False)
    assert refused.status_code == 403

    client.get("/admin/websites", headers=auth)  # gives the client its CSRF cookie
    token = client.cookies.get("csrf_token")
    assert token
    ok = client.post(url, headers=auth, data={"csrf_token": token}, follow_redirects=False)
    assert ok.status_code == 303
