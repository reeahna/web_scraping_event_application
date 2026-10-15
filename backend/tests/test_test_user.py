"""The development/staging test user: one button on the sign-in page."""

import pytest

from app.config import Settings, get_settings
from app.core.permissions import ADMINISTRATOR, REGISTERED_USER
from app.models.user import User


@pytest.fixture
def test_login_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "test_login_enabled", True)
    monkeypatch.setattr(get_settings(), "test_user_email", "tester@bulletin.invalid")


def _test_login(client, **form):
    client.get("/auth/login")
    return client.post(
        "/auth/test-login",
        data={"csrf_token": client.cookies.get("csrf_token"), **form},
        follow_redirects=False,
    )


def test_off_by_default(client):
    assert "Sign in as the test user" not in client.get("/auth/login").text
    assert _test_login(client).status_code == 404


def test_signs_in_as_an_ordinary_user_on_the_homepage(client, db_session, test_login_on):
    assert "Sign in as the test user" in client.get("/auth/login").text

    resp = _test_login(client)
    assert resp.status_code == 303 and resp.headers["location"] == "/"
    user = db_session.query(User).filter_by(email="tester@bulletin.invalid").one()
    assert [ur.role.name for ur in user.user_roles] == [REGISTERED_USER]
    assert client.get("/account").status_code == 200
    assert client.get("/admin", follow_redirects=False).status_code in (303, 403)

    # Signing in again reuses the same account.
    _test_login(client)
    assert db_session.query(User).filter_by(email="tester@bulletin.invalid").count() == 1


def test_returns_to_a_safe_next_page_only(client, test_login_on):
    assert _test_login(client, next="/account/saved").headers["location"] == "/account/saved"
    assert _test_login(client, next="//evil.example").headers["location"] == "/"


def test_needs_a_csrf_token(client, test_login_on):
    client.get("/auth/login")
    resp = client.post("/auth/test-login", data={"csrf_token": "wrong"}, follow_redirects=False)
    assert resp.status_code in (400, 403)


def test_never_opens_an_admin_account(client, make_user, test_login_on):
    make_user(email="tester@bulletin.invalid", role_name=ADMINISTRATOR)
    resp = _test_login(client)
    assert resp.status_code == 403
    assert client.get("/account", follow_redirects=False).status_code in (303, 401)


def test_refused_in_production():
    with pytest.raises(ValueError, match="TEST_LOGIN_ENABLED"):
        Settings(app_env="production", test_login_enabled=True)
