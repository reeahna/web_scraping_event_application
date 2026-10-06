"""Forgot password, reset, and email verification."""

import re
from datetime import UTC, datetime, timedelta

import pytest

import app.services.account_email as account_email
import app.services.email as email_mod
from app.core.security import verify_password
from app.models.user import User
from app.models.user_session import UserSession
from app.models.user_token import UserToken
from app.services import rate_limit
from app.services.email import MemoryEmailSender

PASSWORD = "correct-horse-battery"
NEW_PASSWORD = "a-brand-new-password"


@pytest.fixture
def mailbox(monkeypatch):
    """Email 'configured': everything sent lands here instead of the network."""
    box = MemoryEmailSender()
    monkeypatch.setattr(email_mod, "get_email_sender", lambda settings: box)
    monkeypatch.setattr(account_email, "get_email_sender", lambda settings: box)
    return box


def _link(message, path):
    return re.search(rf"https?://\S+({re.escape(path)}\?token=\S+)", message.body).group(1)


def _csrf(client):
    client.get("/auth/login")
    return client.cookies.get("csrf_token")


def _request_reset(client, email):
    return client.post(
        "/auth/forgot-password", data={"email": email, "csrf_token": _csrf(client)}
    )


def _submit_reset(client, path, password=NEW_PASSWORD, confirm=None):
    token = path.split("token=", 1)[1]
    return client.post(
        "/auth/reset-password",
        data={
            "token": token,
            "password": password,
            "password_confirm": confirm if confirm is not None else password,
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )


# --- without email configured ------------------------------------------------


def test_nothing_is_offered_until_email_is_set_up(client, make_user, login):
    assert "Forgot your password?" not in client.get("/auth/login").text
    page = client.get("/auth/forgot-password").text
    assert "isn't available on this site yet" in page
    assert "<form" not in page.split("<main", 1)[1].split("</main>")[0]


# --- password reset ------------------------------------------------------------


def test_the_full_reset_flow(client, make_user, login, mailbox, db_session):
    user = make_user(email="me@example.com", password=PASSWORD)
    assert "Forgot your password?" in client.get("/auth/login").text

    resp = _request_reset(client, "Me@Example.com")
    assert "we've emailed it a link" in resp.text
    assert len(mailbox.sent) == 1 and mailbox.sent[0].to == "me@example.com"
    path = _link(mailbox.sent[0], "/auth/reset-password")
    assert client.get(path).status_code == 200

    resp = _submit_reset(client, path)
    assert resp.status_code == 303 and resp.headers["location"] == "/auth/login"
    db_session.expire_all()
    assert verify_password(NEW_PASSWORD, db_session.get(User, user.id).hashed_password)
    assert login("me@example.com", NEW_PASSWORD).status_code == 303
    assert login("me@example.com", PASSWORD).status_code == 401


def test_unknown_addresses_get_the_same_answer_and_no_email(client, make_user, mailbox):
    make_user(email="me@example.com", password=PASSWORD)
    known = _request_reset(client, "me@example.com").text
    unknown = _request_reset(client, "nobody@example.com").text
    assert known.replace("me@example.com", "X") == unknown.replace("nobody@example.com", "X")
    assert len(mailbox.sent) == 1


def test_a_link_works_once(client, make_user, mailbox):
    make_user(email="me@example.com", password=PASSWORD)
    _request_reset(client, "me@example.com")
    path = _link(mailbox.sent[0], "/auth/reset-password")
    assert _submit_reset(client, path).status_code == 303
    assert _submit_reset(client, path, password="another-password-1").status_code == 400
    assert "expired or has already been used" in client.get(path).text


def test_only_the_newest_link_works(client, make_user, mailbox):
    make_user(email="me@example.com", password=PASSWORD)
    _request_reset(client, "me@example.com")
    _request_reset(client, "me@example.com")
    first, second = (_link(m, "/auth/reset-password") for m in mailbox.sent)
    assert _submit_reset(client, first).status_code == 400
    assert _submit_reset(client, second).status_code == 303


def test_an_expired_link_is_refused(client, make_user, mailbox, db_session):
    make_user(email="me@example.com", password=PASSWORD)
    _request_reset(client, "me@example.com")
    db_session.query(UserToken).update(
        {UserToken.expires_at: datetime.now(UTC) - timedelta(minutes=1)}
    )
    db_session.commit()
    path = _link(mailbox.sent[0], "/auth/reset-password")
    assert _submit_reset(client, path).status_code == 400


def test_the_new_password_follows_the_sign_up_rules(client, make_user, mailbox):
    make_user(email="me@example.com", password=PASSWORD)
    _request_reset(client, "me@example.com")
    path = _link(mailbox.sent[0], "/auth/reset-password")
    assert _submit_reset(client, path, password="short").status_code == 422
    resp = _submit_reset(client, path, confirm="does-not-match-1")
    assert resp.status_code == 422 and "Passwords do not match" in resp.text
    # A rejected attempt does not use the link up.
    assert _submit_reset(client, path).status_code == 303


def test_resetting_signs_out_every_session(client, make_user, login, mailbox, db_session):
    user = make_user(email="me@example.com", password=PASSWORD)
    login("me@example.com", PASSWORD)
    assert db_session.query(UserSession).filter_by(user_id=user.id).count() == 1
    _request_reset(client, "me@example.com")
    _submit_reset(client, _link(mailbox.sent[0], "/auth/reset-password"))
    assert db_session.query(UserSession).filter_by(user_id=user.id).count() == 0


def test_a_reset_unlocks_a_locked_out_account(client, make_user, login, mailbox):
    make_user(email="me@example.com", password=PASSWORD)
    for _ in range(rate_limit.LOGIN_FAILURES_PER_ACCOUNT):
        login("me@example.com", "wrong")
    assert "or reset your password" in login("me@example.com", PASSWORD).text
    _request_reset(client, "me@example.com")
    _submit_reset(client, _link(mailbox.sent[0], "/auth/reset-password"))
    assert login("me@example.com", NEW_PASSWORD).status_code == 303


def test_reset_emails_to_one_address_are_capped(client, make_user, mailbox):
    make_user(email="me@example.com", password=PASSWORD)
    for _ in range(6):
        _request_reset(client, "me@example.com")
    assert len(mailbox.sent) == 3


def test_tokens_are_stored_hashed(client, make_user, mailbox, db_session):
    make_user(email="me@example.com", password=PASSWORD)
    _request_reset(client, "me@example.com")
    raw = _link(mailbox.sent[0], "/auth/reset-password").split("token=")[1]
    assert db_session.query(UserToken).filter_by(id=raw).count() == 0


def test_reset_links_use_the_configured_site_address_not_the_host_header(
    client, make_user, mailbox
):
    make_user(email="me@example.com", password=PASSWORD)
    client.post(
        "/auth/forgot-password",
        data={"email": "me@example.com", "csrf_token": _csrf(client)},
        headers={"host": "evil.example"},
    )
    assert "evil.example" not in mailbox.sent[0].body


# --- email verification ---------------------------------------------------------


def test_sign_up_sends_a_confirmation_link(client, register, mailbox, db_session):
    register(email="new@example.com")
    assert mailbox.sent[0].to == "new@example.com"
    user = db_session.query(User).filter_by(email="new@example.com").one()
    assert user.email_verified_at is None
    assert "Not confirmed" in client.get("/account").text

    resp = client.get(_link(mailbox.sent[0], "/auth/verify-email"), follow_redirects=False)
    assert resp.status_code == 303
    db_session.expire_all()
    assert db_session.get(User, user.id).email_verified_at is not None
    assert "Confirmed" in client.get("/account").text


def test_sign_up_works_without_email_and_sends_nothing(client, register, db_session):
    resp = register(email="new@example.com")
    assert resp.status_code == 303
    page = client.get("/account").text
    assert "Not confirmed" in page
    assert "Send confirmation link" not in page


def test_a_bad_confirmation_link_says_so(client):
    resp = client.get("/auth/verify-email?token=nope")
    assert resp.status_code == 400
    assert "didn't work" in resp.text


def test_resending_a_confirmation_link(client, register, mailbox):
    register(email="new@example.com")
    csrf = client.cookies.get("csrf_token")
    client.post("/account/verify-email", data={"csrf_token": csrf})
    assert len(mailbox.sent) == 2
    # The first link no longer works; the newest does.
    assert client.get(_link(mailbox.sent[0], "/auth/verify-email")).status_code == 400
    assert client.get(
        _link(mailbox.sent[1], "/auth/verify-email"), follow_redirects=False
    ).status_code == 303


def test_a_reset_link_cannot_be_used_to_verify(client, make_user, mailbox):
    make_user(email="me@example.com", password=PASSWORD)
    _request_reset(client, "me@example.com")
    raw = _link(mailbox.sent[0], "/auth/reset-password").split("token=")[1]
    assert client.get(f"/auth/verify-email?token={raw}").status_code == 400
