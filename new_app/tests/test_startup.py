from app.main import app


def test_app_has_expected_title():
    assert app.title == "New City Events App"


def test_expected_routes_are_registered(client):
    assert client.get("/").status_code == 200
    assert client.get("/health").status_code == 200
    # Static mount is wired up even if no file exists at this path.
    assert client.get("/static/does-not-exist.js").status_code == 404
