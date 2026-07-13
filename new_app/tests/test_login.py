from app.core.permissions import REGISTERED_USER
from app.models.audit_log import AuditLog
from app.models.user import User


def test_successful_login_sets_session_cookie_and_updates_last_login(client, make_user, db_session):
    make_user(email="alice@example.com", password="s3cret-pass", role_name=REGISTERED_USER)

    client.get("/auth/login")
    csrf = client.cookies.get("csrf_token")
    resp = client.post(
        "/auth/login",
        data={"email": "alice@example.com", "password": "s3cret-pass", "csrf_token": csrf},
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/account"
    assert "session_token" in resp.cookies

    db_session.expire_all()
    user = db_session.query(User).filter_by(email="alice@example.com").first()
    assert user.last_login_at is not None

    entries = db_session.query(AuditLog).filter(AuditLog.action == "login").all()
    assert len(entries) == 1
    assert entries[0].user_id == user.id


def test_failed_login_wrong_password_records_audit(client, make_user, db_session):
    make_user(email="bob@example.com", password="correct-password")

    client.get("/auth/login")
    csrf = client.cookies.get("csrf_token")
    resp = client.post(
        "/auth/login",
        data={"email": "bob@example.com", "password": "wrong-password", "csrf_token": csrf},
        follow_redirects=False,
    )

    assert resp.status_code == 401
    assert "session_token" not in resp.cookies

    entries = db_session.query(AuditLog).filter(AuditLog.action == "login_failed").all()
    assert len(entries) == 1
    assert "bob@example.com" in (entries[0].detail or "")
    assert "wrong-password" not in (entries[0].detail or "")


def test_failed_login_unknown_email_records_audit(client, db_session):
    client.get("/auth/login")
    csrf = client.cookies.get("csrf_token")
    resp = client.post(
        "/auth/login",
        data={"email": "nobody@example.com", "password": "whatever123", "csrf_token": csrf},
        follow_redirects=False,
    )

    assert resp.status_code == 401
    entries = db_session.query(AuditLog).filter(AuditLog.action == "login_failed").all()
    assert len(entries) == 1
    assert entries[0].user_id is None
