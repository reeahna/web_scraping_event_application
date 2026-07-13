def test_home_shows_admin_login_link_when_unauthenticated(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert 'href="/auth/login"' in resp.text
    assert 'href="/admin"' not in resp.text


def test_home_shows_admin_and_logout_links_when_authenticated(client, make_user, login):
    make_user(email="ivy@example.com", password="pw-ivy123456")
    login("ivy@example.com", "pw-ivy123456")

    resp = client.get("/")
    assert resp.status_code == 200
    assert 'href="/admin"' in resp.text
    assert 'action="/auth/logout"' in resp.text
    assert 'href="/auth/login"' not in resp.text


def test_home_does_not_require_login(client):
    resp = client.get("/")
    assert resp.status_code == 200
