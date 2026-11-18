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


def test_a_scraped_title_cannot_break_out_of_the_json_ld_block(
    client, published_event, db_session
):
    """The block is rendered raw, so a title carrying "</script>" must not end
    it early and inject markup into the page."""
    published_event.title = "Evil </script><img src=x id=pwned> Show"
    db_session.commit()
    html = client.get(f"/events/{published_event.id}").text

    assert "<img src=x id=pwned>" not in html
    block = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    assert json.loads(block.group(1))["name"] == "Evil </script><img src=x id=pwned> Show"


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
    lines = body.splitlines()
    for prefix in ("/admin", "/account", "/auth", "/register"):
        # The bare path, so /admin itself is covered and not only /admin/...
        assert f"Disallow: {prefix}" in lines
    assert "Sitemap: http" in body


def test_sitemap_lists_public_pages(client, published_event, monkeypatch):
    monkeypatch.setattr(seo, "MIN_INDEXABLE_TOWN_EVENTS", 1)
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


# --- thin town pages -----------------------------------------------------------


def test_a_town_with_too_few_events_is_kept_out_of_search(client, published_event):
    """One upcoming event is below the threshold: the page still works, but
    says noindex and stays out of the sitemap."""
    html = client.get("/city/bloomington").text
    assert '<meta name="robots" content="noindex, follow" />' in html
    assert "/city/bloomington</loc>" not in client.get("/sitemap.xml").text


def test_a_town_with_enough_events_is_indexed(client, published_event, monkeypatch):
    monkeypatch.setattr(seo, "MIN_INDEXABLE_TOWN_EVENTS", 1)
    html = client.get("/city/bloomington").text
    assert '<meta name="robots" content="index, follow" />' in html
    assert "/city/bloomington</loc>" in client.get("/sitemap.xml").text


# --- pagination ------------------------------------------------------------------


def test_town_page_links_do_not_repeat_the_town_id(client, published_event):
    """The town is in the path; ?city_id=N made a second URL for each page."""
    html = client.get("/city/bloomington").text
    assert "city_id=" not in re.sub(r'data-map-url="[^"]*"', "", html)
    # The map still needs the town to fetch its points.
    assert f"/events/map?city_id={published_event.city_id}" in client.get(
        "/city/bloomington?view=map"
    ).text


def test_later_pages_are_their_own_canonical(client, published_event):
    html = client.get("/city/bloomington?page=2").text
    assert re.search(r'<link rel="canonical" href="[^"]*/city/bloomington\?page=2"', html)
    html = client.get("/city/bloomington?q=jazz").text
    assert re.search(r'<link rel="canonical" href="[^"]*/city/bloomington"', html)


# --- titles, images, icons -----------------------------------------------------


def test_event_title_names_the_town_and_day(client, published_event):
    html = client.get(f"/events/{published_event.id}").text
    day = published_event.start_date
    expected = f"Jazz Night · Bloomington, {day.strftime('%b')} {day.day} | "
    assert f"<title>{expected}" in html


def test_pages_without_a_picture_share_the_site_card(client, published_event):
    html = client.get("/").text
    assert re.search(r'<meta property="og:image" content="http[^"]*/static/og-default.png"', html)
    assert client.get("/static/og-default.png").status_code == 200


def test_favicon_is_served(client):
    response = client.get("/favicon.ico", follow_redirects=False)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert client.get("/static/favicon.svg").status_code == 200
    assert 'rel="icon" href="/static/favicon.svg"' in client.get("/").text


def test_signed_out_suggest_link_goes_through_sign_in(client):
    html = client.get("/").text
    assert 'href="/auth/login?next=%2Fsuggest">Suggest a town' in html


# --- "things to do in <town>" pages ---------------------------------------------


def test_town_page_is_titled_for_the_searches_people_make(client, published_event):
    html = client.get("/city/bloomington").text
    assert "<title>Events in Bloomington: things to do this week | " in html
    assert "<h1>Things to do in Bloomington</h1>" in html
    # One plain sentence a search engine can match "events in <town>" against.
    assert "1 upcoming event in Bloomington" in html


def test_this_weekend_and_today_are_pages_of_their_own(client, published_event):
    for window, words in (("this-weekend", "this weekend"), ("today", "today")):
        response = client.get(f"/city/bloomington/{window}")
        assert response.status_code == 200
        html = response.text
        assert f"<title>Things to do in Bloomington {words} | " in html
        assert f"<h1>Things to do in Bloomington {words}</h1>" in html
        assert re.search(
            rf'<link rel="canonical" href="[^"]*/city/bloomington/{window}"', html
        )
    # The quick links point at the pages, not at ?preset= variants of the town.
    html = client.get("/city/bloomington").text
    assert 'href="/city/bloomington/this-weekend"' in html
    assert "?preset=" not in html


def test_a_window_page_shows_only_its_own_dates(
    client, published_event, db_session, make_event
):
    """The event five days out may or may not fall this weekend, so check the
    two ends that cannot: today's event is listed on /today, and an event a
    month away is on neither window page."""
    city = published_event.city
    make_event(city, website=published_event.website, title="Today Talk",
               start_date=current_public_date())
    make_event(city, website=published_event.website, title="Far Future Fair",
               start_date=current_public_date() + timedelta(days=40))
    db_session.commit()
    today_html = client.get("/city/bloomington/today").text
    assert "Today Talk" in today_html
    assert "Far Future Fair" not in today_html
    assert "Far Future Fair" not in client.get("/city/bloomington/this-weekend").text
    assert "Far Future Fair" in client.get("/city/bloomington").text


def test_window_page_pagination_and_map_keep_the_window(client, published_event):
    html = client.get("/city/bloomington/this-weekend?view=map").text
    assert "/events/map?city_id=" in html and "preset=weekend" in html
    # The page's own links do not repeat the window it already names.
    assert "preset=" not in re.sub(r'data-map-url="[^"]*"', "", html)


def test_window_pages_redirect_a_miscased_town(client, published_event):
    response = client.get("/city/BLOOMINGTON/today", follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == "/city/bloomington/today"


def test_window_pages_follow_their_town_in_and_out_of_search(
    client, published_event, monkeypatch
):
    html = client.get("/city/bloomington/this-weekend").text
    assert '<meta name="robots" content="noindex, follow" />' in html
    assert "/city/bloomington/this-weekend</loc>" not in client.get("/sitemap.xml").text

    monkeypatch.setattr(seo, "MIN_INDEXABLE_TOWN_EVENTS", 1)
    html = client.get("/city/bloomington/this-weekend").text
    assert '<meta name="robots" content="index, follow" />' in html
    sitemap = client.get("/sitemap.xml").text
    assert "/city/bloomington/this-weekend</loc>" in sitemap
    assert "/city/bloomington/today</loc>" in sitemap


def test_town_page_links_to_nearby_towns(client, published_event, db_session, make_city):
    town = published_event.city
    town.default_latitude, town.default_longitude = 39.1653, -86.5264  # Bloomington, IN
    near = make_city(name="West Lafayette", slug="west-lafayette")
    near.default_latitude, near.default_longitude = 40.4259, -86.9081
    far = make_city(name="Eugene", slug="eugene")
    far.default_latitude, far.default_longitude = 44.0521, -123.0868
    db_session.commit()

    html = client.get("/city/bloomington").text
    assert "Events near Bloomington" in html
    assert 'href="/city/west-lafayette"' in html
    assert 'href="/city/eugene"' not in html


def test_town_without_coordinates_lists_no_nearby_towns(client, published_event):
    assert "Events near Bloomington" not in client.get("/city/bloomington").text


def test_town_and_event_pages_carry_breadcrumbs(client, published_event):
    def blocks(html):
        return [
            json.loads(b)
            for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
        ]

    town = blocks(client.get("/city/bloomington/this-weekend").text)
    crumbs = next(b for b in town if b["@type"] == "BreadcrumbList")
    assert [c["name"] for c in crumbs["itemListElement"]] == [
        "Towns", "Bloomington", "This weekend"
    ]
    assert all(c["item"].startswith("http") for c in crumbs["itemListElement"])

    event = blocks(client.get(f"/events/{published_event.id}").text)
    assert event[0]["@type"] == "Event"
    crumbs = next(b for b in event if b["@type"] == "BreadcrumbList")
    assert [c["name"] for c in crumbs["itemListElement"]] == [
        "Towns", "Bloomington", "Jazz Night"
    ]


def test_nearby_towns_is_nearest_first_and_capped():
    class Town:
        def __init__(self, id, lat, lon, active=True):
            self.id, self.default_latitude, self.default_longitude = id, lat, lon
            self.is_active = active

    home = Town(1, 42.28, -83.74)  # Ann Arbor
    towns = [
        home,
        Town(2, 42.73, -84.48),  # East Lansing, ~50 mi
        Town(3, 41.88, -87.63),  # Chicago, ~200 mi
        Town(4, 42.33, -83.05),  # Detroit, ~35 mi
        Town(5, 42.30, -83.70, active=False),
        Town(6, None, None),
    ]
    found = seo.nearby_towns(home, towns, limit=2)
    assert [t.id for t, _ in found] == [4, 2]
    assert 30 < found[0][1] < 40
