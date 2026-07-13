def test_login_without_csrf_token_is_rejected(client, make_user):
    make_user(email="csrftest@example.com", password="pw-csrf123456")
    client.get("/auth/login")  # sets the csrf cookie, but the form sends a bogus value

    resp = client.post(
        "/auth/login",
        data={
            "email": "csrftest@example.com",
            "password": "pw-csrf123456",
            "csrf_token": "missing-or-blank",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_login_with_mismatched_csrf_token_is_rejected(client, make_user):
    make_user(email="csrftest2@example.com", password="pw-csrf123456")
    client.get("/auth/login")

    resp = client.post(
        "/auth/login",
        data={
            "email": "csrftest2@example.com",
            "password": "pw-csrf123456",
            "csrf_token": "not-the-real-token",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_admin_action_without_valid_csrf_token_is_rejected(
    client, make_super_admin, make_user, login
):
    make_super_admin(email="csrfadmin@example.com", password="pw-csrfadmin1")
    target = make_user(email="csrftarget@example.com", password="pw-csrftarget1")
    login("csrfadmin@example.com", "pw-csrfadmin1")

    resp = client.post(
        f"/admin/users/{target.id}/deactivate",
        data={"csrf_token": "wrong-token"},
        follow_redirects=False,
    )
    assert resp.status_code == 403
