"""/login is a temporary redirect to the real sign-in page, /auth/login."""

from urllib.parse import parse_qs, urlparse


def test_login_redirects_to_the_sign_in_page(client):
    resp = client.get("/login", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/auth/login"


def test_login_keeps_a_local_next_path(client):
    resp = client.get("/login?next=/account/saved", follow_redirects=False)
    location = urlparse(resp.headers["location"])
    assert location.path == "/auth/login"
    assert parse_qs(location.query) == {"next": ["/account/saved"]}


def test_login_drops_a_next_that_leaves_the_site(client):
    for unsafe in ("https://evil.example/", "//evil.example", "/\\evil.example"):
        resp = client.get("/login", params={"next": unsafe}, follow_redirects=False)
        assert resp.headers["location"] == "/auth/login"


def test_following_login_reaches_sign_in_and_signing_in_returns_to_next(client, login):
    page = client.get("/login?next=/account")
    assert page.status_code == 200
    assert "Sign in" in page.text
    done = login("reader@example.com", next="/account")
    assert done.status_code in (302, 303)
    assert done.headers["location"] == "/account"
