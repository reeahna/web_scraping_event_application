from app.core.permissions import REGISTERED_USER


def test_admin_dashboard_requires_login(client):
    resp = client.get("/admin")
    assert resp.status_code == 401


def test_admin_dashboard_accessible_with_admin_permission(client, make_super_admin, login):
    make_super_admin(email="frank@example.com", password="pw-frank12345")
    login("frank@example.com", "pw-frank12345")

    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "frank@example.com" in resp.text


def test_browser_request_to_protected_page_redirects_to_login(client):
    resp = client.get("/admin", headers={"accept": "text/html"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/auth/login")
    assert "next=" in resp.headers["location"]


def test_api_style_request_to_protected_page_still_gets_401(client):
    resp = client.get("/admin", headers={"accept": "application/json"}, follow_redirects=False)
    assert resp.status_code == 401
    assert resp.json()["detail"]


def test_login_after_redirect_lands_on_originally_requested_page(client, make_super_admin, login):
    make_super_admin(email="grace@example.com", password="pw-grace12345")

    redirect_resp = client.get(
        "/admin/users", headers={"accept": "text/html"}, follow_redirects=False
    )
    assert redirect_resp.status_code == 303
    login_url = redirect_resp.headers["location"]
    assert login_url.startswith("/auth/login?next=")

    # Simulate following that redirect, then logging in from that page.
    client.get(login_url)
    csrf = client.cookies.get("csrf_token")
    login_resp = client.post(
        "/auth/login",
        data={
            "email": "grace@example.com",
            "password": "pw-grace12345",
            "csrf_token": csrf,
            "next": "/admin/users",
        },
        follow_redirects=False,
    )
    assert login_resp.status_code == 303
    assert login_resp.headers["location"] == "/admin/users"


def test_next_param_rejects_absolute_or_protocol_relative_urls(client, make_user):
    make_user(
        email="henry@example.com",
        password="pw-henry12345",
        role_name=REGISTERED_USER,
    )

    client.get("/auth/login")
    csrf = client.cookies.get("csrf_token")
    resp = client.post(
        "/auth/login",
        data={
            "email": "henry@example.com",
            "password": "pw-henry12345",
            "csrf_token": csrf,
            "next": "//evil.example.com/phish",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/account"
