"""Sign-in input a provider or a link controls: an overlong ?next= or display
name must never stop someone signing in."""

from app.core.email import normalize_email
from app.models.user import User
from app.services import oauth_login
from app.services.oauth import ExternalIdentityInfo, MockProvider


def test_an_overlong_next_falls_back_to_the_homepage():
    assert oauth_login.safe_next("/" + "a" * 600) == "/"
    assert oauth_login.safe_next("/events/1") == "/events/1"


def test_an_overlong_next_does_not_break_sign_in_start(client, monkeypatch):
    info = ExternalIdentityInfo(
        provider="google", subject="test:x", email="x@example.com",
        email_verified=True, display_name=None, avatar_url=None,
    )
    monkeypatch.setattr(oauth_login, "is_enabled", lambda settings, name: True)
    monkeypatch.setattr(oauth_login, "build_provider", lambda settings, name: MockProvider(info))
    response = client.get("/auth/oauth/google?next=/" + "a" * 600, follow_redirects=False)
    assert response.status_code in (302, 303, 307)


def test_a_huge_provider_display_name_is_trimmed_not_fatal(
    client, monkeypatch, db_session
):
    info = ExternalIdentityInfo(
        provider="google", subject="test:long-name", email="long-name@example.com",
        email_verified=True, display_name="N\x00" + "x" * 300, avatar_url=None,
    )
    monkeypatch.setattr(oauth_login, "is_enabled", lambda settings, name: True)
    monkeypatch.setattr(oauth_login, "build_provider", lambda settings, name: MockProvider(info))
    client.get("/auth/login")
    start = client.get("/auth/oauth/google", follow_redirects=False)
    state = start.headers["location"].split("state=")[1].split("&")[0]
    client.get(f"/auth/oauth/google/callback?code=c&state={state}", follow_redirects=False)

    user = db_session.query(User).filter(User.email == normalize_email(info.email)).one()
    assert user.full_name == "N" + "x" * 254
