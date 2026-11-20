"""Sitemap coverage and dates, search engine ownership tags, and the site's
Organization data.

The production sitemap stopped at exactly 10,000 events with no dates, and the
site could not be verified in Search Console: the onrender.com address takes no
DNS record, so only the meta tag method works.
"""

import json
import re
from datetime import UTC, datetime, time, timedelta

import pytest

from app.config import get_settings
from app.repositories.public_events import current_public_date
from app.routers import seo as seo_router
from app.services import seo


@pytest.fixture
def public_events(db_session, make_city, make_website, make_event):
    city = make_city(name="Ithaca", slug="ithaca-ny", state_or_region="NY")
    website = make_website(city, name="Cornell")
    website.is_active = True
    website.active_configuration_version = 1
    db_session.commit()
    day = current_public_date() + timedelta(days=2)
    events = [
        make_event(
            city, website=website, title=f"Event {i}",
            canonical_url=f"https://example.org/{i}", start_date=day, start_time=time(18, i),
        )
        for i in range(4)
    ]
    db_session.commit()
    return city, events


def _locs(body):
    return re.findall(r"<loc>(.*?)</loc>", body)


def test_every_public_event_and_indexable_town_is_listed(client, public_events, monkeypatch):
    monkeypatch.setattr(seo, "MIN_INDEXABLE_TOWN_EVENTS", 1)
    city, events = public_events
    locs = _locs(client.get("/sitemap.xml").text)
    for event in events:
        assert any(loc.endswith(f"/events/{event.id}") for loc in locs)
    for suffix in ("", "/this-weekend", "/today"):
        assert any(loc.endswith(f"/city/ithaca-ny{suffix}") for loc in locs)
    assert len(locs) == len(set(locs)), "a URL is listed twice"


def test_sitemap_urls_are_the_canonical_urls(client, public_events, monkeypatch):
    """Every <loc> is exactly the canonical link its page declares."""
    monkeypatch.setattr(seo, "MIN_INDEXABLE_TOWN_EVENTS", 1)
    for loc in _locs(client.get("/sitemap.xml").text):
        path = re.sub(r"^https?://[^/]+", "", loc) or "/"
        page = client.get(path)
        assert page.status_code == 200, path
        canonical = re.search(r'<link rel="canonical" href="([^"]+)"', page.text).group(1)
        assert canonical == loc
        assert 'content="noindex' not in page.text


def test_the_cap_leaves_room_for_the_whole_catalogue(client, public_events, monkeypatch):
    """The cap counts every URL in the file, not only events, so the file can
    never pass the protocol's 50,000."""
    assert seo_router.MAX_SITEMAP_URLS <= 50_000
    monkeypatch.setattr(seo_router, "MAX_SITEMAP_URLS", 6)
    locs = _locs(client.get("/sitemap.xml").text)
    assert len(locs) == 6
    assert sum("/events/" in loc for loc in locs) == 2


def test_events_carry_lastmod_when_their_details_changed(client, public_events, db_session):
    _, events = public_events
    changed = events[0]
    changed.content_updated_at = datetime(2026, 11, 19, 15, 30, tzinfo=UTC)
    events[1].content_updated_at = None
    db_session.commit()

    body = client.get("/sitemap.xml").text
    assert (
        f"/events/{changed.id}</loc><lastmod>2026-11-19T15:30:00+00:00</lastmod>" in body
    )
    assert f"/events/{events[1].id}</loc></url>" in body


def test_a_hidden_town_stays_out_of_the_sitemap(client, public_events):
    """Below the event threshold the town is noindex, and stays unlisted."""
    body = client.get("/sitemap.xml").text
    assert "/city/ithaca-ny</loc>" not in body


def test_robots_points_at_the_sitemap(client):
    body = client.get("/robots.txt").text
    assert re.search(r"^Sitemap: https?://\S+/sitemap\.xml$", body, re.M)


def test_no_verification_tags_unless_configured(client):
    html = client.get("/").text
    assert "google-site-verification" not in html
    assert "msvalidate.01" not in html


def test_verification_tags_appear_on_every_page_when_set(client, public_events, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "google_site_verification", "abc123-token")
    monkeypatch.setattr(settings, "bing_site_verification", "BING42")
    city, events = public_events
    for path in ("/", "/city/ithaca-ny", f"/events/{events[0].id}"):
        html = client.get(path).text
        assert '<meta name="google-site-verification" content="abc123-token" />' in html
        assert '<meta name="msvalidate.01" content="BING42" />' in html


def test_a_verification_token_cannot_inject_markup(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "google_site_verification", '"><script>x</script>')
    html = client.get("/").text
    assert "<script>x</script>" not in html


def test_home_page_names_the_organization(client):
    html = client.get("/").text
    blocks = [
        json.loads(b)
        for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    ]
    org = next(b for b in blocks if b["@type"] == "Organization")
    site = next(b for b in blocks if b["@type"] == "WebSite")
    assert org["name"] == get_settings().app_name
    assert org["url"].startswith("http")
    assert org["logo"].endswith(".png")
    assert "address" not in org  # a website, not a place
    assert site["publisher"]["@id"] == org["@id"]
