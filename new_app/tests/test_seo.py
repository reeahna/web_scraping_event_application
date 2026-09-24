"""Search-engine and social metadata for the public site.

The public pages previously shared one meta description, had no canonical
links, no social preview tags, no structured data, and no robots.txt or
sitemap.
"""

import json
import re
from datetime import date, time, timedelta

import pytest

from app.repositories.public_events import current_public_date
from app.services import seo


@pytest.fixture
def published_event(db_session, make_city, make_website, make_event):
    """An event the public site will actually serve."""
    city = make_city(name="Bloomington", slug="bloomington")
    website = make_website(city, name="Buskirk-Chumley")
    website.is_active = True
    website.active_configuration_version = 1
    db_session.commit()
    event = make_event(
        city,
        website=website,
        title="Jazz Night",
        canonical_url="https://example.org/jazz",
        start_date=current_public_date() + timedelta(days=5),
        start_time=time(19, 30),
        venue="The Bishop",
        address="123 Walnut St",
        description="An evening of live jazz.",
    )
    db_session.commit()
    return event


# --- structured data ---------------------------------------------------------


def test_event_page_carries_schema_org_json_ld(client, published_event):
    html = client.get(f"/events/{published_event.id}").text
    match = re.search(
        r'<script type="application/ld\+json">(.*?)</script>', html, re.S
    )
    assert match, "no JSON-LD block on the event page"
    data = json.loads(match.group(1))
    assert data["@type"] == "Event"
    assert data["name"] == "Jazz Night"
    assert data["location"]["name"] == "The Bishop"
    assert data["url"].startswith("http")


def test_structured_data_is_valid_json_even_with_awkward_text(
    db_session, make_city, make_event
):
    """Titles come from scraped pages, so they contain quotes and angle
    brackets; the payload has to survive them."""
    city = make_city(name="Quote City", slug="quote-city")
    event = make_event(
        city,
        title='An "Evening" of <jazz> & more',
        canonical_url="https://example.org/q",
        description="Line one\nLine two",
    )
    db_session.commit()
    data = json.loads(seo.event_structured_data(event))
    assert data["name"] == 'An "Evening" of <jazz> & more'


def test_an_end_is_only_declared_when_one_is_known(db_session, make_city, make_event):
    """Falling back to the start date emitted a date-only endDate for a timed
    event, which reads as "ends at midnight" rather than "end unknown"."""
    city = make_city(name="End City", slug="end-city")
    event = make_event(
        city, title="No End", canonical_url="https://example.org/ne",
        start_date=date(2026, 10, 3), start_time=time(19, 30),
    )
    db_session.commit()
    assert "endDate" not in json.loads(seo.event_structured_data(event))

    event.end_time = time(22, 0)
    db_session.commit()
    assert json.loads(seo.event_structured_data(event))["endDate"] == "2026-10-03T22:00:00"


def test_a_cancelled_event_says_so(db_session, make_city, make_event):
    city = make_city(name="Cancel City", slug="cancel-city")
    event = make_event(city, title="Off", canonical_url="https://example.org/off")
    event.is_cancelled = True
    db_session.commit()
    data = json.loads(seo.event_structured_data(event))
    assert data["eventStatus"] == "https://schema.org/EventCancelled"


# --- head metadata -----------------------------------------------------------


def test_pages_have_their_own_description(client, published_event):
    home = client.get("/").text
    event = client.get(f"/events/{published_event.id}").text

    def description(html):
        return re.search(r'<meta name="description" content="([^"]*)"', html).group(1)

    assert description(home) != description(event)
    assert "Jazz Night" in description(event)


def test_every_public_page_declares_a_canonical_url(client, published_event):
    for path in ("/", f"/events/{published_event.id}"):
        html = client.get(path).text
        canonical = re.search(r'<link rel="canonical" href="([^"]*)"', html).group(1)
        assert canonical.startswith("http"), path


def test_canonical_ignores_the_host_header(client, published_event):
    """A Host header is attacker-controlled; deriving canonical links from it
    would let a visitor decide what a search engine records."""
    html = client.get("/", headers={"Host": "evil.example.com"}).text
    canonical = re.search(r'<link rel="canonical" href="([^"]*)"', html).group(1)
    assert "evil.example.com" not in canonical


def test_social_preview_tags_are_present(client, published_event):
    html = client.get(f"/events/{published_event.id}").text
    for prop in ("og:title", "og:description", "og:url", "og:type"):
        assert f'property="{prop}"' in html, prop
    assert 'name="twitter:card"' in html


# --- robots and sitemap -------------------------------------------------------


def test_robots_disallows_the_private_areas(client):
    body = client.get("/robots.txt").text
    for prefix in ("/admin/", "/account/", "/auth/"):
        assert f"Disallow: {prefix}" in body
    assert "Sitemap: http" in body


def test_sitemap_lists_public_pages(client, published_event):
    body = client.get("/sitemap.xml").text
    assert body.startswith("<?xml")
    assert f"/events/{published_event.id}</loc>" in body
    assert "/city/bloomington</loc>" in body


def test_sitemap_never_advertises_a_hidden_event(client, published_event, db_session):
    """It shares the listing's visibility rules, so deactivating an event has
    to remove it from the sitemap too."""
    assert f"/events/{published_event.id}<" in client.get("/sitemap.xml").text

    published_event.is_active = False
    db_session.commit()
    assert f"/events/{published_event.id}<" not in client.get("/sitemap.xml").text


def test_sitemap_contains_nothing_private(client, published_event):
    body = client.get("/sitemap.xml").text
    for prefix in ("/admin", "/account", "/auth"):
        assert prefix not in body
