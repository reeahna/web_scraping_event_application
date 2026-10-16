"""Regressions for small public-site gaps: town URLs typed in the wrong case,
and HEAD requests from uptime monitors."""

from datetime import timedelta

from app.repositories.public_events import current_public_date

TODAY = current_public_date()
TOMORROW = TODAY + timedelta(days=1)
YESTERDAY = TODAY - timedelta(days=1)


def _town(make_city, make_website, **city_kwargs):
    city = make_city(**city_kwargs)
    website = make_website(city, is_active=True, approved_pattern={"pattern_name": "static_html"})
    return city, website


class TestTownSlugCase:
    def test_town_page_redirects_to_canonical_slug(self, client, make_city, make_website):
        _town(make_city, make_website, name="Bloomington", slug="bloomington")

        resp = client.get("/city/BLOOMINGTON?q=jazz&page=2", follow_redirects=False)

        assert resp.status_code == 301
        assert resp.headers["location"] == "/city/bloomington?q=jazz&page=2"

    def test_calendar_feed_redirects_to_canonical_slug(self, client, make_city, make_website):
        _town(make_city, make_website, name="Bloomington", slug="bloomington")

        resp = client.get("/city/Bloomington/events.ics", follow_redirects=False)

        assert resp.status_code == 301
        assert resp.headers["location"] == "/city/bloomington/events.ics"
        assert client.get(resp.headers["location"]).status_code == 200

    def test_exact_slug_is_served_directly(self, client, make_city, make_website):
        _town(make_city, make_website, name="Bloomington", slug="bloomington")
        assert client.get("/city/bloomington", follow_redirects=False).status_code == 200

    def test_unknown_or_inactive_town_is_still_404(self, client, make_city, make_website):
        _town(make_city, make_website, name="Closed", slug="closed", is_active=False)
        assert client.get("/city/CLOSED", follow_redirects=False).status_code == 404
        assert client.get("/city/NOWHERE", follow_redirects=False).status_code == 404
        assert client.get("/city/NOWHERE/events.ics", follow_redirects=False).status_code == 404


class TestHead:
    def test_get_routes_answer_head_without_a_body(
        self, client, make_city, make_website, make_event
    ):
        city, website = _town(make_city, make_website, name="Bloomington", slug="bloomington")
        event = make_event(city, website=website, title="Head Check", start_date=TOMORROW)

        for path in (
            "/",
            "/robots.txt",
            "/sitemap.xml",
            "/city/bloomington",
            "/city/bloomington/events.ics",
            f"/events/{event.id}",
            "/static/style.css",
        ):
            get = client.get(path, follow_redirects=False)
            head = client.head(path, follow_redirects=False)
            assert head.status_code == get.status_code == 200, path
            assert head.content == b"", path
            assert head.headers["content-type"] == get.headers["content-type"], path
            # The other middlewares still run for HEAD.
            assert "x-correlation-id" in head.headers, path
            assert "content-security-policy" in head.headers, path

    def test_head_keeps_status_of_missing_and_redirecting_pages(
        self, client, make_city, make_website
    ):
        _town(make_city, make_website, name="Bloomington", slug="bloomington")
        missing = client.head("/city/nowhere")
        assert missing.status_code == 404
        assert missing.content == b""
        moved = client.head("/city/BLOOMINGTON", follow_redirects=False)
        assert moved.status_code == 301
        assert moved.headers["location"] == "/city/bloomington"

    def test_post_only_route_still_rejects_head(self, client):
        resp = client.head("/auth/logout")
        assert resp.status_code == 405
        assert resp.content == b""
