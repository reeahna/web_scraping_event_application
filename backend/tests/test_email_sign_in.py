"""Signing in with a 6-digit code sent by email: the codes table, the sign-in
history, the limits, and the sign-in pages."""

import re
import smtplib
from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings, get_settings
from app.core.permissions import REGISTERED_USER, SUPER_ADMINISTRATOR
from app.models.email_sign_in import EmailSignInCode, SignInHistory
from app.models.external_identity import ExternalIdentity
from app.models.user import User
from app.routers import auth as auth_router
from app.services import email as email_service
from app.services import email_sign_in
from app.services.account_deletion import delete_account
from app.services.email import (
    MemoryEmailSender,
    NoopEmailSender,
    SmtpEmailSender,
    email_sign_in_available,
    get_email_sender,
    get_sign_in_email_sender,
)


class FailingSender:
    name = "failing"

    def send(self, message):
        return False


@pytest.fixture
def outbox(monkeypatch):
    """Email sign-in switched on, with codes captured instead of sent."""
    sender = MemoryEmailSender()
    monkeypatch.setattr(auth_router, "email_sign_in_available", lambda settings: True)
    monkeypatch.setattr(auth_router, "get_sign_in_email_sender", lambda settings: sender)
    return sender


def _code_from(message) -> str:
    return re.search(r"\b(\d{6})\b", message.body).group(1)


def _request(client, email, next=None):
    client.get("/auth/login")
    data = {"csrf_token": client.cookies.get("csrf_token"), "email": email}
    if next:
        data["next"] = next
    return client.post("/auth/email/request", data=data, follow_redirects=False)


def _verify(client, code, next=None):
    data = {"csrf_token": client.cookies.get("csrf_token"), "code": code}
    if next:
        data["next"] = next
    return client.post("/auth/email/verify", data=data, follow_redirects=False)


def _roles(user):
    return {ur.role.name for ur in user.user_roles}


# --- the full flow -----------------------------------------------------------------


def test_new_address_gets_a_code_and_signs_in(client, outbox, db_session):
    resp = _request(client, "  New.Person@Example.com ")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/email/verify"
    assert len(outbox.sent) == 1
    message = outbox.sent[0]
    assert message.to == "new.person@example.com"
    code = _code_from(message)

    page = client.get("/auth/email/verify")
    assert page.status_code == 200
    assert "new.person@example.com" in page.text
    assert 'autocomplete="one-time-code"' in page.text

    done = _verify(client, code)
    assert done.status_code == 303
    assert done.headers["location"] == "/"
    assert client.cookies.get(get_settings().session_cookie_name)

    user = db_session.query(User).filter_by(email="new.person@example.com").one()
    assert user.email_verified_at is not None
    assert _roles(user) == {REGISTERED_USER}
    assert client.get("/account").status_code == 200


def test_the_code_is_stored_hashed_and_expires_in_ten_minutes(client, outbox, db_session):
    _request(client, "a@example.com")
    code = _code_from(outbox.sent[0])
    row = db_session.query(EmailSignInCode).one()
    assert code not in row.code_hash
    created = row.created_at.replace(tzinfo=None)
    expires = row.expires_at.replace(tzinfo=None)
    assert expires - created == timedelta(minutes=10)
    assert email_sign_in.code_matches(row.code_hash, "a@example.com", code)
    assert not email_sign_in.code_matches(row.code_hash, "b@example.com", code)


def test_next_is_carried_through(client, outbox):
    resp = _request(client, "a@example.com", next="/account/saved")
    assert resp.headers["location"] == "/auth/email/verify?next=/account/saved"
    done = _verify(client, _code_from(outbox.sent[0]), next="/account/saved")
    assert done.headers["location"] == "/account/saved"


def test_an_unsafe_next_is_dropped(client, outbox):
    _request(client, "a@example.com", next="https://evil.example/")
    done = _verify(client, _code_from(outbox.sent[0]), next="//evil.example")
    assert done.headers["location"] == "/"


def test_existing_google_account_signs_in_with_a_code(client, outbox, login, db_session):
    login("member@example.com")
    client.cookies.clear()
    _request(client, "member@example.com")
    _verify(client, _code_from(outbox.sent[0]))
    assert db_session.query(User).filter_by(email="member@example.com").count() == 1


def test_a_code_works_only_once(client, outbox):
    _request(client, "a@example.com")
    code = _code_from(outbox.sent[0])
    assert _verify(client, code).status_code == 303
    client.cookies.set("email_sign_in_address", "a@example.com", path="/auth")
    again = _verify(client, code)
    assert again.status_code == 400
    assert "expired or is no longer valid" in again.text


# --- only the newest code works -------------------------------------------------------


def test_requesting_again_invalidates_the_first_code(client, outbox, db_session):
    _request(client, "a@example.com")
    _request(client, "a@example.com")
    first, second = (_code_from(m) for m in outbox.sent)
    if first == second:  # one in a million; nothing to compare
        pytest.skip("same code drawn twice")
    rows = db_session.query(EmailSignInCode).order_by(EmailSignInCode.id).all()
    assert rows[0].invalidated_at is not None
    assert rows[1].invalidated_at is None

    bad = _verify(client, first)
    assert bad.status_code == 400
    assert _verify(client, second).status_code == 303


def test_the_resend_button_on_the_code_page(client, outbox):
    _request(client, "a@example.com")
    page = client.get("/auth/email/verify")
    assert 'action="/auth/email/request"' in page.text
    assert 'name="email" value="a@example.com"' in page.text


# --- expiry and wrong guesses ---------------------------------------------------------


def test_an_expired_code_is_refused(client, outbox, db_session):
    _request(client, "a@example.com")
    row = db_session.query(EmailSignInCode).one()
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()
    resp = _verify(client, _code_from(outbox.sent[0]))
    assert resp.status_code == 400
    assert "expired" in resp.text
    assert db_session.query(User).filter_by(email="a@example.com").count() == 0


def test_wrong_codes_count_down_then_kill_the_code(client, outbox, db_session):
    _request(client, "a@example.com")
    code = _code_from(outbox.sent[0])
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    for left in (4, 3, 2, 1):
        resp = _verify(client, wrong)
        assert resp.status_code == 400
        assert f"{left} {'try' if left == 1 else 'tries'} left" in resp.text
    resp = _verify(client, wrong)
    assert "too many times" in resp.text
    # Even the right code no longer works.
    assert _verify(client, code).status_code == 400
    row = db_session.query(EmailSignInCode).one()
    assert row.failed_attempts == 5 and row.invalidated_at is not None


def test_a_malformed_code_is_not_counted(client, outbox, db_session):
    _request(client, "a@example.com")
    resp = _verify(client, "12ab")
    assert resp.status_code == 400
    assert "6-digit code" in resp.text
    assert db_session.query(EmailSignInCode).one().failed_attempts == 0


def test_spaces_in_the_code_are_allowed(client, outbox):
    _request(client, "a@example.com")
    code = _code_from(outbox.sent[0])
    assert _verify(client, f"{code[:3]} {code[3:]}").status_code == 303


# --- send limits -------------------------------------------------------------------


def test_codes_per_address_are_limited(client, outbox, monkeypatch):
    monkeypatch.setattr(get_settings(), "email_codes_per_address_per_hour", 2)
    assert _request(client, "a@example.com").status_code == 303
    assert _request(client, "a@example.com").status_code == 303
    resp = _request(client, "a@example.com")
    assert resp.status_code == 429
    assert "Too many codes" in resp.text
    assert len(outbox.sent) == 2


def test_codes_per_ip_are_limited(client, outbox, monkeypatch):
    monkeypatch.setattr(get_settings(), "email_codes_per_ip_per_hour", 2)
    assert _request(client, "a@example.com").status_code == 303
    assert _request(client, "b@example.com").status_code == 303
    assert _request(client, "c@example.com").status_code == 429
    assert len(outbox.sent) == 2


def test_code_checks_per_ip_are_limited(client, outbox, monkeypatch):
    monkeypatch.setattr(get_settings(), "email_code_checks_per_ip_per_15_minutes", 1)
    _request(client, "a@example.com")
    code = _code_from(outbox.sent[0])
    _verify(client, f"{(int(code) + 1) % 1_000_000:06d}")
    assert _verify(client, code).status_code == 429


def test_an_invalid_address_is_refused(client, outbox):
    for bad in ("", "not-an-email", "a@b", "a b@example.com", "x" * 250 + "@example.com"):
        resp = _request(client, bad)
        assert resp.status_code == 400, bad
        assert "valid email address" in resp.text
    assert outbox.sent == []


def test_a_failed_send_says_so_and_leaves_no_working_code(client, monkeypatch, db_session):
    monkeypatch.setattr(auth_router, "email_sign_in_available", lambda settings: True)
    monkeypatch.setattr(auth_router, "get_sign_in_email_sender", lambda s: FailingSender())
    resp = _request(client, "a@example.com")
    assert resp.status_code == 503
    assert "send the email just now" in resp.text
    assert db_session.query(EmailSignInCode).one().invalidated_at is not None


# --- accounts that may not sign in ------------------------------------------------


def test_a_disabled_account_gets_no_code(client, outbox, make_user):
    make_user("off@example.com", is_active=False)
    resp = _request(client, "off@example.com")
    # Same answer as for anyone else, but nothing is sent.
    assert resp.status_code == 303
    assert outbox.sent == []


def test_closed_registration_sends_no_code_to_a_new_address(client, outbox, monkeypatch):
    monkeypatch.setattr(get_settings(), "registration_enabled", False)
    assert _request(client, "new@example.com").status_code == 303
    assert outbox.sent == []


def test_closed_registration_still_lets_an_existing_account_in(
    client, outbox, monkeypatch, make_user
):
    monkeypatch.setattr(get_settings(), "registration_enabled", False)
    make_user("old@example.com")
    _request(client, "old@example.com")
    assert _verify(client, _code_from(outbox.sent[0])).status_code == 303


def test_superadmin_email_gets_the_role(client, outbox, monkeypatch, db_session):
    monkeypatch.setattr(get_settings(), "superadmin_email", "Owner@Example.com")
    _request(client, "owner@example.com")
    done = _verify(client, _code_from(outbox.sent[0]))
    assert done.headers["location"] == "/admin"
    user = db_session.query(User).filter_by(email="owner@example.com").one()
    assert SUPER_ADMINISTRATOR in _roles(user)


def test_a_code_unlinks_a_social_sign_in_that_never_verified_the_address(
    client, outbox, login, db_session
):
    login("victim@example.com", provider="facebook", email_verified=False)
    client.cookies.clear()
    _request(client, "victim@example.com")
    _verify(client, _code_from(outbox.sent[0]))
    assert db_session.query(ExternalIdentity).count() == 0


# --- sign-in history ------------------------------------------------------------------


def test_history_records_sends_rejections_and_sign_ins(client, outbox, login, db_session):
    _request(client, "a@example.com")
    code = _code_from(outbox.sent[0])
    _verify(client, f"{(int(code) + 1) % 1_000_000:06d}")
    _verify(client, code)
    client.cookies.clear()
    login("g@example.com")

    rows = db_session.query(SignInHistory).order_by(SignInHistory.id).all()
    assert [(r.email, r.method, r.outcome) for r in rows] == [
        ("a@example.com", "email_code", "code_sent"),
        ("a@example.com", "email_code", "code_rejected"),
        ("a@example.com", "email_code", "signed_in"),
        ("g@example.com", "google", "signed_in"),
    ]
    user = db_session.query(User).filter_by(email="a@example.com").one()
    assert rows[2].user_id == user.id


def test_deleting_the_account_deletes_its_codes_and_history(client, outbox, db_session):
    _request(client, "a@example.com")
    _verify(client, _code_from(outbox.sent[0]))
    user = db_session.query(User).filter_by(email="a@example.com").one()
    delete_account(db_session, user)
    assert db_session.query(SignInHistory).count() == 0
    assert db_session.query(EmailSignInCode).count() == 0


# --- the sign-in page and switches -----------------------------------------------------


def test_the_email_form_is_hidden_and_routes_404_when_off(client):
    page = client.get("/auth/login")
    assert "/auth/email/request" not in page.text
    csrf = client.cookies.get("csrf_token")
    resp = client.post(
        "/auth/email/request", data={"csrf_token": csrf, "email": "a@example.com"}
    )
    assert resp.status_code == 404
    assert client.get("/auth/email/verify").status_code == 404


def test_the_email_form_shows_when_on(client, outbox):
    page = client.get("/auth/login")
    assert 'action="/auth/email/request"' in page.text
    assert 'autocomplete="email"' in page.text


def test_the_code_page_without_a_recent_request_goes_back(client, outbox):
    resp = client.get("/auth/email/verify", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/auth/login"


def test_requests_need_a_csrf_token(client, outbox):
    client.get("/auth/login")
    resp = client.post(
        "/auth/email/request", data={"csrf_token": "wrong", "email": "a@example.com"}
    )
    assert resp.status_code in (400, 403)
    assert outbox.sent == []


def test_switches_choose_the_sender():
    base = Settings(_env_file=None)
    assert isinstance(get_sign_in_email_sender(base), NoopEmailSender)
    assert not email_sign_in_available(base)

    smtp = Settings(
        _env_file=None, email_sign_in_enabled=True, email_backend="smtp",
        smtp_host="smtp.example.com",
    )
    assert isinstance(get_sign_in_email_sender(smtp), SmtpEmailSender)
    assert email_sign_in_available(smtp)
    # Alert emails stay off unless EMAIL_ENABLED is on too.
    assert isinstance(get_email_sender(smtp), NoopEmailSender)

    no_host = Settings(_env_file=None, email_sign_in_enabled=True, email_backend="smtp")
    assert not email_sign_in_available(no_host)


def test_console_backend_is_refused_for_codes_in_production():
    with pytest.raises(ValueError, match="EMAIL_BACKEND=console"):
        Settings(
            _env_file=None, app_env="production", email_sign_in_enabled=True,
            email_backend="console",
        )


# --- the SMTP sender ------------------------------------------------------------------


class FakeSmtp:
    instances: list["FakeSmtp"] = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port = host, port
        self.calls: list = []
        FakeSmtp.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, message):
        self.calls.append(("send", message["To"], message["Subject"], message["From"]))


@pytest.fixture
def fake_smtp(monkeypatch):
    FakeSmtp.instances = []
    monkeypatch.setattr(email_service.smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(email_service.smtplib, "SMTP_SSL", FakeSmtp)
    return FakeSmtp


def _smtp_sender(port=587):
    return SmtpEmailSender(
        host="smtp.example.com", port=port, username="me@example.com",
        password="app-password", from_address="me@example.com", from_name="Bulletin",
    )


def test_smtp_uses_starttls_then_logs_in_and_sends(fake_smtp):
    ok = _smtp_sender().send(email_service.EmailMessage("to@example.com", "Hi", "Body"))
    assert ok
    calls = fake_smtp.instances[0].calls
    assert calls[0] == "starttls"
    assert calls[1] == ("login", "me@example.com", "app-password")
    assert calls[2] == ("send", "to@example.com", "Hi", "Bulletin <me@example.com>")


def test_smtp_port_465_uses_implicit_tls(fake_smtp):
    _smtp_sender(465).send(email_service.EmailMessage("to@example.com", "Hi", "Body"))
    assert "starttls" not in fake_smtp.instances[0].calls


def test_smtp_failure_returns_false(monkeypatch):
    def boom(*args, **kwargs):
        raise smtplib.SMTPAuthenticationError(535, b"bad")

    monkeypatch.setattr(email_service.smtplib, "SMTP", boom)
    assert _smtp_sender().send(email_service.EmailMessage("to@example.com", "Hi", "B")) is False
