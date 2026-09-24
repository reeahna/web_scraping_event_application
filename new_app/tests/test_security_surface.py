"""Security properties that are easy to regress and hard to notice.

Each of these was verified by probing the running app, not assumed.
"""

import pytest



@pytest.mark.parametrize("env,expected", [
    ("development", True), ("production", False), ("staging", False), ("test", False),
])
def test_api_docs_are_only_enabled_in_development(env, expected):
    """/openapi.json listed all 96 admin routes to an anonymous caller. The
    docs are served with no authentication at all, so outside development they
    are a free map of the admin surface."""
    from app.main import docs_enabled_for

    assert docs_enabled_for(env) is expected


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_docs_still_available_in_development(client, path):
    assert client.get(path).status_code == 200


def test_session_cookie_is_httponly_and_samesite(client, make_user, login):
    make_user(email="cookie@example.com", password="cookie-pass-123")
    response = login("cookie@example.com", "cookie-pass-123")
    assert response.status_code == 303, response.text[:200]

    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookies if c.startswith("session"))
    assert "httponly" in session_cookie.lower()
    assert "samesite=lax" in session_cookie.lower()


def test_security_headers_are_present(client):
    headers = client.get("/").headers
    assert "Content-Security-Policy" in headers
    assert headers.get("X-Frame-Options") == "DENY"
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert "Referrer-Policy" in headers


def test_csp_does_not_allow_inline_script(client):
    """The whole admin relies on this staying strict; app/static/js exists
    because of it."""
    csp = client.get("/").headers["Content-Security-Policy"]
    script_src = next(part for part in csp.split(";") if part.strip().startswith("script-src"))
    assert "'unsafe-inline'" not in script_src
    assert "'unsafe-eval'" not in script_src


def test_an_anonymous_visitor_cannot_reach_the_admin(client):
    response = client.get("/admin", follow_redirects=False)
    assert response.status_code in (302, 303, 401, 403)
