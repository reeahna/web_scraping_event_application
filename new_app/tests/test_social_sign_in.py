"""Sign-in is only through Google, Microsoft or Facebook: the superadmin
setting, new-account switch, account-hijack guard and the sign-in page."""

import pytest

from app.config import get_settings
from app.core.permissions import REGISTERED_USER, SUPER_ADMINISTRATOR
from app.models.external_identity import ExternalIdentity
from app.models.user import User
from app.services import rate_limit


def _roles(user):
    return {ur.role.name for ur in user.user_roles}


@pytest.fixture
def superadmin_email(monkeypatch):
    monkeypatch.setattr(get_settings(), "superadmin_email", "Owner@Example.com")


# --- SUPERADMIN_EMAIL ---------------------------------------------------------------


def test_the_configured_address_becomes_superadmin_on_verified_sign_in(
    login, db_session, superadmin_email
):
    resp = login("owner@example.com")
    assert resp.headers["location"] == "/admin"
    user = db_session.query(User).filter_by(email="owner@example.com").one()
    assert SUPER_ADMINISTRATOR in _roles(user)


def test_an_unverified_sign_in_with_that_address_gets_nothing(
    login, db_session, superadmin_email
):
    # Facebook never asserts the address is verified, so anyone could claim it.
    login("owner@example.com", provider="facebook", email_verified=False)
    user = db_session.query(User).filter_by(email="owner@example.com").one()
    assert SUPER_ADMINISTRATOR not in _roles(user)
    assert _roles(user) == {REGISTERED_USER}


def test_other_addresses_and_an_unset_setting_grant_nothing(login, db_session, monkeypatch):
    login("someone@example.com")
    monkeypatch.setattr(get_settings(), "superadmin_email", "owner@example.com")
    login("another@example.com")
    for email in ("someone@example.com", "another@example.com"):
        user = db_session.query(User).filter_by(email=email).one()
        assert SUPER_ADMINISTRATOR not in _roles(user)


def test_signing_in_again_does_not_duplicate_the_role(login, db_session, superadmin_email):
    login("owner@example.com")
    login("owner@example.com")
    user = db_session.query(User).filter_by(email="owner@example.com").one()
    assert [ur.role.name for ur in user.user_roles].count(SUPER_ADMINISTRATOR) == 1


# --- account hijack guard ----------------------------------------------------------


def test_a_verified_sign_in_unlinks_an_earlier_unverified_one(
    login, db_session, superadmin_email
):
    """Someone opens a Facebook account under the owner's address and signs in
    first. When the owner arrives through Google (verified), the Facebook link
    is removed, so it cannot reach the account, or its admin role."""
    login("owner@example.com", provider="facebook", email_verified=False)
    login("owner@example.com", provider="google")

    user = db_session.query(User).filter_by(email="owner@example.com").one()
    providers = {i.provider for i in db_session.query(ExternalIdentity).filter_by(user_id=user.id)}
    assert providers == {"google"}
    assert SUPER_ADMINISTRATOR in _roles(user)


# --- new accounts on/off -----------------------------------------------------------------


def test_with_new_accounts_off_strangers_are_refused_but_members_and_owner_get_in(
    login, make_user, db_session, monkeypatch
):
    monkeypatch.setattr(get_settings(), "registration_enabled", False)
    monkeypatch.setattr(get_settings(), "superadmin_email", "owner@example.com")
    make_user(email="member@example.com", role_name=REGISTERED_USER)

    assert login("stranger@example.com").headers["location"] == "/auth/login"
    assert db_session.query(User).filter_by(email="stranger@example.com").count() == 0
    assert login("member@example.com").headers["location"] == "/account"
    assert login("owner@example.com").headers["location"] == "/admin"


# --- the sign-in page and old links ------------------------------------------------------


def test_sign_in_page_lists_providers_and_has_no_password_field(client, monkeypatch):
    import app.routers.auth as auth_router

    monkeypatch.setattr(
        auth_router, "enabled_providers", lambda settings: ["google", "microsoft"]
    )
    page = client.get("/auth/login").text
    assert 'href="/auth/oauth/google"' in page and 'href="/auth/oauth/microsoft"' in page
    assert 'type="password"' not in page


def test_sign_in_page_says_so_when_no_provider_is_set_up(client):
    assert "Sign-in isn't set up on this site yet." in client.get("/auth/login").text


def test_old_sign_up_link_goes_to_sign_in(client):
    resp = client.get("/register", follow_redirects=False)
    assert resp.status_code == 301 and resp.headers["location"] == "/auth/login"


def test_password_login_is_gone(client):
    client.get("/auth/login")
    resp = client.post(
        "/auth/login",
        data={
            "email": "x@example.com",
            "password": "x",
            "csrf_token": client.cookies.get("csrf_token"),
        },
    )
    assert resp.status_code == 405


def test_sign_in_starts_are_limited_per_address(client, monkeypatch):
    from app.services import oauth_login
    from app.services.oauth import ExternalIdentityInfo, MockProvider

    info = ExternalIdentityInfo(provider="google", subject="s", email="a@example.com",
                                email_verified=True, display_name=None, avatar_url=None)
    monkeypatch.setattr(oauth_login, "is_enabled", lambda settings, name: True)
    monkeypatch.setattr(oauth_login, "build_provider", lambda settings, name: MockProvider(info))
    limit = get_settings().sign_in_starts_per_15_minutes
    for _ in range(limit):
        assert client.get("/auth/oauth/google", follow_redirects=False).status_code == 303
    assert client.get("/auth/oauth/google", follow_redirects=False).status_code == 429
    assert rate_limit.SIGN_IN_WINDOW_SECONDS == 15 * 60


def test_providers_return_people_to_the_sites_own_address_by_default():
    from types import SimpleNamespace

    from app.services.oauth_login import _redirect_uri

    site = SimpleNamespace(public_base_url="https://bulletin.example", oauth_redirect_base_url=None)
    assert _redirect_uri(site, "google") == "https://bulletin.example/auth/oauth/google/callback"
    override = SimpleNamespace(
        public_base_url="https://bulletin.example", oauth_redirect_base_url="https://other.example/"
    )
    assert _redirect_uri(override, "google") == "https://other.example/auth/oauth/google/callback"


def test_readiness_flags_a_site_nobody_can_sign_in_to():
    from app.core.production_checks import production_blockers

    codes = {i.code for i in production_blockers(get_settings())}
    assert "no_sign_in_provider" in codes and "no_superadmin_email" in codes


def test_the_real_google_client_builds_a_sign_in_link():
    """The real provider (not the test double) loads and builds Google's
    authorization URL offline, so a missing library shows up here, not as a
    500 on the live sign-in button."""
    from types import SimpleNamespace
    from urllib.parse import parse_qs, urlparse

    from app.services.oauth import build_provider

    settings = SimpleNamespace(google_client_id="id-123", google_client_secret="secret")
    url = build_provider(settings, "google").authorization_url(
        state="s1", nonce="n1", redirect_uri="https://example.test/auth/oauth/google/callback"
    )
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.netloc == "accounts.google.com"
    assert query["client_id"] == ["id-123"] and query["state"] == ["s1"]
    assert query["nonce"] == ["n1"]
    assert query["redirect_uri"] == ["https://example.test/auth/oauth/google/callback"]
