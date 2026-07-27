"""Phase 14: OAuth login resolution and security rules (mocked provider)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.permissions import REGISTERED_USER
from app.models.external_identity import ExternalIdentity, OAuthLoginState
from app.services import oauth_login
from app.services.oauth import ExternalIdentityInfo, MockProvider

SETTINGS = SimpleNamespace(oauth_redirect_base_url="http://testserver")
NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _info(**over):
    base = dict(
        provider="google", subject="sub-123", email="new@example.com",
        email_verified=True, display_name="New Person", avatar_url="http://x/a.png",
    )
    base.update(over)
    return ExternalIdentityInfo(**base)


def _start(db, info):
    provider = MockProvider(info)
    url = oauth_login.start_login(
        db, SETTINGS, info.provider, next_url="/account", provider=provider, now=NOW
    )
    state = db.query(OAuthLoginState).filter(OAuthLoginState.provider == info.provider).one().state
    return provider, state, url


def test_start_login_stores_state_and_builds_url(db_session):
    provider, state, url = _start(db_session, _info())
    assert state in url
    assert db_session.query(OAuthLoginState).count() == 1


def test_new_user_is_created_for_a_verified_identity(db_session):
    info = _info()
    provider, state, _ = _start(db_session, info)
    user, next_url = oauth_login.complete_login(
        db_session, SETTINGS, "google", code="c", state=state, provider=provider, now=NOW
    )
    assert user.email == "new@example.com"
    assert user.hashed_password is None  # no third-party password stored
    assert next_url == "/account"
    roles = {ur.role.name for ur in user.user_roles}
    assert REGISTERED_USER in roles
    # State is one-time: consumed.
    assert db_session.query(OAuthLoginState).count() == 0


def test_verified_email_links_to_existing_user(db_session, make_user):
    existing = make_user(email="me@example.com", password="password12345",
                         role_name=REGISTERED_USER)
    info = _info(email="me@example.com", subject="g-1")
    provider, state, _ = _start(db_session, info)
    user, _ = oauth_login.complete_login(
        db_session, SETTINGS, "google", code="c", state=state, provider=provider, now=NOW
    )
    assert user.id == existing.id  # linked, not duplicated
    identity = db_session.query(ExternalIdentity).one()
    assert identity.user_id == existing.id


def test_unverified_email_does_not_hijack_existing_account(db_session, make_user):
    make_user(email="victim@example.com", password="password12345", role_name=REGISTERED_USER)
    info = _info(email="victim@example.com", subject="g-2", email_verified=False)
    provider, state, _ = _start(db_session, info)
    with pytest.raises(oauth_login.OAuthError) as exc:
        oauth_login.complete_login(
            db_session, SETTINGS, "google", code="c", state=state, provider=provider, now=NOW
        )
    assert exc.value.code == "email_unverified_conflict"


def test_returning_identity_logs_in_same_user(db_session):
    info = _info(subject="g-3")
    # First login creates the user + identity.
    p1, s1, _ = _start(db_session, info)
    user1, _ = oauth_login.complete_login(
        db_session, SETTINGS, "google", code="c", state=s1, provider=p1, now=NOW
    )
    # Second login with the same subject returns the same user (no duplicate).
    p2, s2, _ = _start(db_session, info)
    user2, _ = oauth_login.complete_login(
        db_session, SETTINGS, "google", code="c", state=s2, provider=p2, now=NOW
    )
    assert user1.id == user2.id
    assert db_session.query(ExternalIdentity).count() == 1


def test_disabled_user_is_rejected(db_session, make_user):
    user = make_user(email="off@example.com", password="password12345",
                     role_name=REGISTERED_USER)
    user.is_active = False
    db_session.add(
        ExternalIdentity(user_id=user.id, provider="google", subject="g-4")
    )
    db_session.commit()
    info = _info(subject="g-4", email="off@example.com")
    provider, state, _ = _start(db_session, info)
    with pytest.raises(oauth_login.OAuthError) as exc:
        oauth_login.complete_login(
            db_session, SETTINGS, "google", code="c", state=state, provider=provider, now=NOW
        )
    assert exc.value.code == "account_disabled"


def test_invalid_state_is_rejected(db_session):
    with pytest.raises(oauth_login.OAuthError) as exc:
        oauth_login.complete_login(
            db_session, SETTINGS, "google", code="c", state="nope",
            provider=MockProvider(_info()), now=NOW,
        )
    assert exc.value.code == "invalid_state"


def test_expired_state_is_rejected(db_session):
    info = _info()
    provider, state, _ = _start(db_session, info)
    later = NOW + timedelta(seconds=oauth_login.STATE_TTL_SECONDS + 10)
    with pytest.raises(oauth_login.OAuthError) as exc:
        oauth_login.complete_login(
            db_session, SETTINGS, "google", code="c", state=state, provider=provider, now=later
        )
    assert exc.value.code == "expired_state"


def test_missing_email_is_rejected(db_session):
    info = _info(email=None, email_verified=False, subject="g-5")
    provider, state, _ = _start(db_session, info)
    with pytest.raises(oauth_login.OAuthError) as exc:
        oauth_login.complete_login(
            db_session, SETTINGS, "google", code="c", state=state, provider=provider, now=NOW
        )
    assert exc.value.code == "email_required"


def test_provider_enable_disable():
    disabled = SimpleNamespace(google_client_id=None, google_client_secret=None)
    assert oauth_login.is_enabled(disabled, "google") is False
    enabled = SimpleNamespace(google_client_id="id", google_client_secret="secret")
    assert oauth_login.is_enabled(enabled, "google") is True
    assert oauth_login.enabled_providers(enabled) == ["google"]
    # Unknown provider is never enabled.
    assert oauth_login.is_enabled(enabled, "bogus") is False
