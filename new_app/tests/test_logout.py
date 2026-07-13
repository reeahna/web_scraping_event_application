from app.models.audit_log import AuditLog


def test_logout_clears_session_and_blocks_further_access(client, make_user, login, db_session):
    make_user(email="carol@example.com", password="pw-carol123")
    login_resp = login("carol@example.com", "pw-carol123")
    assert login_resp.status_code == 303

    csrf = client.cookies.get("csrf_token")
    logout_resp = client.post("/auth/logout", data={"csrf_token": csrf}, follow_redirects=False)
    assert logout_resp.status_code == 303

    protected = client.get("/admin")
    assert protected.status_code == 401

    entries = db_session.query(AuditLog).filter(AuditLog.action == "logout").all()
    assert len(entries) == 1
