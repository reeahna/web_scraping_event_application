"""Behind Render's TLS proxy the app sees plain http. A redirect Starlette
builds from the request (such as /city/x/ -> /city/x) then pointed at http://
and cost a second hop; HttpsSchemeMiddleware marks those requests as https."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.middleware import HttpsSchemeMiddleware


def _app(with_middleware: bool) -> FastAPI:
    app = FastAPI()

    @app.get("/city/{slug}")
    def city(slug: str):
        return {"slug": slug}

    if with_middleware:
        app.add_middleware(HttpsSchemeMiddleware)
    return app


def test_trailing_slash_redirect_stays_on_https():
    client = TestClient(_app(True), base_url="http://example.org")
    response = client.get("/city/ann-arbor/", follow_redirects=False)
    assert response.status_code in (307, 308)
    assert response.headers["location"] == "https://example.org/city/ann-arbor"


def test_without_it_the_redirect_drops_to_http():
    client = TestClient(_app(False), base_url="http://example.org")
    response = client.get("/city/ann-arbor/", follow_redirects=False)
    assert response.headers["location"].startswith("http://")
