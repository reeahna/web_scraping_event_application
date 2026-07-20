from app.core.permissions import REGISTERED_USER


def test_home_shows_shared_login_and_registration_links_when_unauthenticated(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'href="/auth/login"' in resp.text
    assert "Log In" in resp.text
    assert "Admin Login" not in resp.text
    assert 'href="/register"' in resp.text
    assert 'href="/admin"' not in resp.text


def test_home_shows_account_and_logout_but_not_admin_for_registered_user(client, make_user, login):
    make_user(email="ivy@example.com", password="pw-ivy123456", role_name=REGISTERED_USER)
    login("ivy@example.com", "pw-ivy123456")

    resp = client.get("/")
    assert resp.status_code == 200
    assert 'href="/account"' in resp.text
    assert 'href="/admin"' not in resp.text
    assert 'action="/auth/logout"' in resp.text
    assert 'href="/auth/login"' not in resp.text


def test_home_shows_admin_link_and_account_navigation_for_user_with_admin_access(
    client, make_super_admin, login
):
    make_super_admin(email="admin-nav@example.com", password="pw-admin123456")
    login("admin-nav@example.com", "pw-admin123456")

    resp = client.get("/")
    assert resp.status_code == 200
    assert 'href="/account"' in resp.text
    assert 'href="/admin"' in resp.text
    assert "Admin Dashboard" not in resp.text
    assert 'action="/auth/logout"' in resp.text


def test_home_does_not_require_login(client):
    resp = client.get("/")
    assert resp.status_code == 200


def test_site_name_links_to_home_on_public_page(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert '<a href="/" class="logo">New City Events</a>' in resp.text


def test_site_name_links_to_home_on_admin_page(client, make_super_admin, login):
    make_super_admin(email="logo-check@example.com", password="pw-logo123456")
    login("logo-check@example.com", "pw-logo123456")

    resp = client.get("/admin")
    assert resp.status_code == 200
    assert '<a href="/" class="logo">New City Events</a>' in resp.text
