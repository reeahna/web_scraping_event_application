from app.models.audit_log import AuditLog


def test_login_audit_record_has_required_fields(client, make_user, db_session):
    make_user(email="audit@example.com", password="pw-audit12345")

    client.get("/auth/login")
    csrf = client.cookies.get("csrf_token")
    client.post(
        "/auth/login",
        data={"email": "audit@example.com", "password": "pw-audit12345", "csrf_token": csrf},
        follow_redirects=False,
    )

    entry = db_session.query(AuditLog).filter(AuditLog.action == "login").one()
    assert entry.user_id is not None
    assert entry.action == "login"
    assert entry.entity_type == "user"
    assert entry.entity_id is not None
    assert entry.created_at is not None
    assert entry.correlation_id is not None
    assert entry.ip_address is not None


def test_audit_records_never_contain_password_or_csrf_secret(client, make_user, db_session):
    password = "do-not-log-me-1234"
    make_user(email="secretcheck@example.com", password=password)

    client.get("/auth/login")
    csrf = client.cookies.get("csrf_token")
    client.post(
        "/auth/login",
        data={"email": "secretcheck@example.com", "password": password, "csrf_token": csrf},
        follow_redirects=False,
    )

    entries = db_session.query(AuditLog).all()
    assert len(entries) > 0
    for entry in entries:
        for field in (entry.detail, entry.before_state, entry.after_state):
            if field:
                assert password not in field
                assert csrf not in field
