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


def test_login_after_redirect_lands_on_originally_requested_page(
    client, make_super_admin, login, monkeypatch
):
    import app.routers.auth as auth_router

    monkeypatch.setattr(auth_router, "enabled_providers", lambda settings: ["google"])
    make_super_admin(email="grace@example.com")

    redirect_resp = client.get(
        "/admin/users", headers={"accept": "text/html"}, follow_redirects=False
    )
    assert redirect_resp.status_code == 303
    login_url = redirect_resp.headers["location"]
    assert login_url.startswith("/auth/login?next=")

    # The sign-in page carries the destination into each provider's link...
    page = client.get(login_url).text
    assert 'href="/auth/oauth/google?next=/admin/users"' in page

    # ...and the provider round-trip returns there.
    login_resp = login("grace@example.com", next="/admin/users")
    assert login_resp.status_code == 303
    assert login_resp.headers["location"] == "/admin/users"


def test_next_param_rejects_absolute_or_protocol_relative_urls(make_user, login):
    make_user(email="henry@example.com", role_name=REGISTERED_USER)

    resp = login("henry@example.com", next="//evil.example.com/phish")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/account"