"""Sharing an event to social platforms from its public page."""

import html
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

TOMORROW = datetime.now(UTC).date() + timedelta(days=1)


def _event(make_city, make_website, make_event, **overrides):
    city = make_city()
    website = make_website(city, is_active=True, approved_pattern={"pattern_name": "x"})
    return city, make_event(city, website=website, start_date=TOMORROW, **overrides)


def _share_links(page: str) -> dict[str, str]:
    block = page.split('class="event-share"', 1)[1].split("</div>", 1)[0]
    return {
        key: html.unescape(href)
        for key, href in re.findall(r'class="share-pill share-(\w+)" href="([^"]+)"', block)
    }


class TestShareRow:
    def test_signed_out_visitor_gets_share_links(
        self, client, make_city, make_website, make_event
    ):
        _, event = _event(make_city, make_website, make_event, title="Jazz & Blues Night")
        page = client.get(f"/events/{event.id}").text

        links = _share_links(page)
        assert set(links) == {
            "x", "facebook", "bluesky", "linkedin", "reddit", "whatsapp", "email"
        }
        assert '/static/js/event-share.js' in page
        # The JS-only buttons start hidden so they never appear dead without JS.
        assert re.search(r'class="share-pill share-native" hidden', page)
        assert re.search(r'class="share-pill share-copy" hidden', page)

    def test_links_carry_the_configured_event_url_and_title(
        self, client, make_city, make_website, make_event
    ):
        city, event = _event(make_city, make_website, make_event, title="Jazz & Blues Night")
        page = client.get(f"/events/{event.id}").text
        links = _share_links(page)

        canonical = re.search(r'<link rel="canonical" href="([^"]+)"', page)
        event_url = (
            html.unescape(canonical.group(1)) if canonical else f"/events/{event.id}"
        )
        assert event_url.endswith(f"/events/{event.id}")

        x = parse_qs(urlsplit(links["x"]).query)
        assert x["url"] == [event_url]
        assert x["text"][0].startswith("Jazz & Blues Night")
        assert city.name in x["text"][0]

        assert parse_qs(urlsplit(links["facebook"]).query)["u"] == [event_url]
        assert parse_qs(urlsplit(links["linkedin"]).query)["url"] == [event_url]
        assert parse_qs(urlsplit(links["reddit"]).query)["title"] == ["Jazz & Blues Night"]
        assert event_url in parse_qs(urlsplit(links["whatsapp"]).query)["text"][0]
        assert links["email"].startswith("mailto:?")
        assert event_url in parse_qs(urlsplit(links["email"]).query)["body"][0]

    def test_share_url_ignores_the_request_host(
        self, client, make_city, make_website, make_event
    ):
        _, event = _event(make_city, make_website, make_event)
        page = client.get(f"/events/{event.id}", headers={"Host": "evil.example"}).text
        assert "evil.example" not in page.split('class="event-share"', 1)[1]

    def test_external_share_links_open_in_new_tab_safely(
        self, client, make_city, make_website, make_event
    ):
        _, event = _event(make_city, make_website, make_event)
        page = client.get(f"/events/{event.id}").text
        block = page.split('class="event-share"', 1)[1].split("</div>", 1)[0]
        for tag in re.findall(r'<a class="share-pill share-(?!email)\w+"[^>]*>', block):
            assert 'target="_blank"' in tag
            assert 'rel="noopener noreferrer"' in tag


class TestSocialPreview:
    def test_event_image_is_the_preview_image(
        self, client, make_city, make_website, make_event
    ):
        _, event = _event(
            make_city, make_website, make_event, image_url="https://example.com/poster.jpg"
        )
        page = client.get(f"/events/{event.id}").text
        assert '<meta property="og:image" content="https://example.com/poster.jpg"' in page
        assert '<meta name="twitter:card" content="summary_large_image"' in page
