from app.models.audit_log import AuditLog


def test_login_audit_record_has_required_fields(make_user, login, db_session):
    make_user(email="audit@example.com")
    login("audit@example.com")

    entry = db_session.query(AuditLog).filter(AuditLog.action == "login").one()
    assert entry.user_id is not None
    assert entry.action == "login"
    assert entry.entity_type == "user"
    assert entry.entity_id is not None
    assert entry.created_at is not None
    assert entry.correlation_id is not None
    assert entry.ip_address is not None


def test_audit_records_never_contain_session_or_csrf_secrets(client, make_user, login, db_session):
    make_user(email="secretcheck@example.com")
    login("secretcheck@example.com")
    secrets = [client.cookies.get("csrf_token"), client.cookies.get("session_token")]
    assert all(secrets)

    entries = db_session.query(AuditLog).all()
    assert len(entries) > 0
    for entry in entries:
        for field in (entry.detail, entry.before_state, entry.after_state):
            for secret in secrets:
                assert secret not in (field or "")