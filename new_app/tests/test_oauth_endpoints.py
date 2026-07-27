"""Phase 14: OAuth endpoints (mocked provider, no network)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from app.services import oauth_login
from app.services.oauth import ExternalIdentityInfo, MockProvider


def _info(**over):
    base = dict(
        provider="google", subject="ep-1", email="ep@example.com",
        email_verified=True, display_name="EP", avatar_url=None,
    )
    base.update(over)
    return ExternalIdentityInfo(**base)


@pytest.fixture
def enable_google(monkeypatch):
    """Enable 'google' and route it to a mocked provider — no real OAuth app."""
    monkeypatch.setattr(oauth_login, "is_enabled", lambda settings, name: name == "google")
    monkeypatch.setattr(oauth_login, "build_provider", lambda settings, name: MockProvider(_info()))


def test_disabled_provider_is_404(client):
    # Default config enables no provider.
    assert client.get("/auth/oauth/google", follow_redirects=False).status_code == 404


def test_start_redirects_to_provider(client, enable_google):
    resp = client.get("/auth/oauth/google?next=/account", follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers["location"]
    assert "mock-provider.test" in location
    assert "state=" in location


def test_full_login_flow_creates_session(client, enable_google):
    start = client.get("/auth/oauth/google?next=/account", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

    callback = client.get(
        f"/auth/oauth/google/callback?code=abc&state={state}", follow_redirects=False
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == "/account"
    # A session cookie was set — the account page is now reachable.
    account = client.get("/account")
    assert account.status_code == 200


def test_callback_with_bad_state_redirects_to_login(client, enable_google):
    resp = client.get(
        "/auth/oauth/google/callback?code=abc&state=bogus", follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_callback_without_code_fails_safely(client, enable_google):
    resp = client.get("/auth/oauth/google/callback?state=x", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"
