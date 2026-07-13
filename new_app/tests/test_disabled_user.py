def test_login_rejected_for_disabled_user(client, make_user):
    make_user(email="dave@example.com", password="pw-dave12345", is_active=False)

    client.get("/auth/login")
    csrf = client.cookies.get("csrf_token")
    resp = client.post(
        "/auth/login",
        data={"email": "dave@example.com", "password": "pw-dave12345", "csrf_token": csrf},
        follow_redirects=False,
    )

    assert resp.status_code == 403
    assert "session_token" not in resp.cookies


def test_active_session_invalidated_when_user_deactivated_mid_session(
    client, make_user, login, db_session
):
    user = make_user(email="erin@example.com", password="pw-erin12345")
    login_resp = login("erin@example.com", "pw-erin12345")
    assert login_resp.status_code == 303

    # Simulate an admin disabling the account while the session is still live.
    user.is_active = False
    db_session.commit()

    resp = client.get("/admin")
    assert resp.status_code == 401
