"""A browser gets a readable error page; API callers keep their JSON."""

import pytest

HTML = {"accept": "text/html,application/xhtml+xml"}


def test_missing_event_shows_a_page_to_a_browser(client):
    resp = client.get("/events/999999", headers=HTML)
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("text/html")
    assert "Page not found" in resp.text
    assert 'href="/"' in resp.text  # a way back
    assert '"detail"' not in resp.text


def test_unknown_url_shows_a_page_to_a_browser(client):
    resp = client.get("/no-such-page", headers=HTML)
    assert resp.status_code == 404
    assert "Page not found" in resp.text


def test_api_callers_still_get_json(client):
    resp = client.get("/events/999999", headers={"accept": "application/json"})
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Event not found"}
    assert client.get("/no-such-page").json() == {"detail": "Not Found"}


def test_an_unhandled_error_shows_a_page_without_internals(monkeypatch):
    from starlette.testclient import TestClient

    import app.routers.home as home
    from app.main import app

    def boom(*args, **kwargs):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(home, "search_public_cities", boom)
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/", headers=HTML)
    assert resp.status_code == 500
    assert "Something went wrong" in resp.text
    assert "secret internal detail" not in resp.text


@pytest.mark.parametrize("path", ["/events/999999", "/no-such-page"])
def test_error_pages_are_not_indexed(client, path):
    assert 'content="noindex"' in client.get(path, headers=HTML).text
