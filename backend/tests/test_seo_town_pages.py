"""Town pages: the local signals search engines use to tie a page to a town.

In production no town had admin coordinates, so the "Events near" links and the
town's geo data never appeared; states showed as "MI" on one town and
"Indiana" on the next; and a big town's description listed every school and
ran to 335 characters.
"""

import json
import re
from datetime import time, timedelta

import pytest

from app.repositories.public_events import current_public_date
from app.services import seo


@pytest.fixture
def town_with_events(db_session, make_city, make_website, make_event):
    """Bloomington with no admin coordinates and six public events at real
    venues, one of them geocoded far away by mistake."""

    def build(name="Bloomington", slug="bloomington-in", state="IN", positions=None, **city):
        town = make_city(name=name, slug=slug, state_or_region=state, **city)
        website = make_website(
            town, name="IU Calendar", base_url="https://events.iu.edu/api/v1/feed.json"
        )
        website.is_active = True
        website.active_configuration_version = 1
        db_session.commit()
        day = current_public_date() + timedelta(days=3)
        for i, (lat, lon) in enumerate(positions or []):
            event = make_event(
                town, website=website, title=f"{name} event {i}",
                canonical_url=f"https://example.org/{slug}/{i}",
                start_date=day, start_time=time(19, 0),
            )
            event.latitude, event.longitude = lat, lon
        db_session.commit()
        return town

    return build


BLOOMINGTON = [
    (39.168, -86.523), (39.171, -86.519), (39.165, -86.530),
    (39.160, -86.525), (39.172, -86.512),
    (41.878, -87.630),  # a venue geocoded to Chicago by mistake
]
WEST_LAFAYETTE = [(40.425, -86.909), (40.428, -86.915), (40.423, -86.920)]


def _blocks(html):
    return [
        json.loads(b)
        for b in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    ]


def test_a_town_is_placed_at_the_median_of_its_events():
    rows = [(1, lat, lon) for lat, lon in BLOOMINGTON]
    lat, lon = seo.centers_from_coordinates(rows)[1]
    # The Chicago outlier does not drag the town out of Indiana.
    assert 39.15 < lat < 39.18
    assert -86.54 < lon < -86.51


def test_too_few_positions_place_no_town():
    assert seo.centers_from_coordinates([(1, 39.1, -86.5), (1, 39.2, -86.6)]) == {}


def test_admin_coordinates_win_over_event_positions(make_city):
    town = make_city(name="A", slug="a", default_latitude=1.0, default_longitude=2.0)
    assert seo.town_center(town, {town.id: (9.0, 9.0)}) == (1.0, 2.0)


def test_nearby_towns_appear_without_admin_coordinates(client, town_with_events):
    town_with_events(positions=BLOOMINGTON)
    town_with_events(
        name="West Lafayette", slug="west-lafayette-in", positions=WEST_LAFAYETTE
    )
    town_with_events(
        name="Eugene", slug="eugene-or", state="OR",
        positions=[(44.05, -123.09), (44.04, -123.07), (44.06, -123.08)],
    )

    html = client.get("/city/bloomington-in").text
    assert "Events near Bloomington" in html
    assert '<a href="/city/west-lafayette-in">West Lafayette, Indiana</a>' in html
    assert 'href="/city/eugene-or"' not in html

    listing = next(b for b in _blocks(html) if b["@type"] == "ItemList")
    geo = listing["about"]["geo"]
    assert 39.15 < geo["latitude"] < 39.18
    assert listing["about"]["containedInPlace"]["name"] == "Indiana"


def test_state_names_are_spelled_out_however_the_town_was_entered(make_city):
    ann_arbor = make_city(name="Ann Arbor", slug="ann-arbor-mi", state_or_region="MI")
    bloomington = make_city(name="Bloomington", slug="bloomington-in", state_or_region="Indiana")
    assert seo.town_place(ann_arbor) == "Ann Arbor, Michigan"
    assert seo.town_place(bloomington) == "Bloomington, Indiana"
    assert seo.town_page_title(ann_arbor) == "Events in Ann Arbor, Michigan: things to do this week"
    assert (
        seo.town_page_title(ann_arbor, "this-weekend")
        == "Things to do in Ann Arbor, Michigan this weekend"
    )


def test_a_region_outside_the_us_is_left_as_entered(make_city):
    town = make_city(name="Waterloo", slug="waterloo", state_or_region="Ontario")
    assert seo.town_place(town) == "Waterloo, Ontario"


@pytest.mark.parametrize("window", [None, "this-weekend", "today"])
def test_descriptions_fit_a_search_result_even_with_many_schools(make_city, window):
    town = make_city(
        name="Atlanta", slug="atlanta-ga", state_or_region="GA",
        universities=[
            "Atlanta Metropolitan State College", "Clark Atlanta University",
            "Emory University", "Georgia Institute of Technology",
            "Georgia State University", "Morehouse College", "Spelman College",
        ],
    )
    text = seo.town_meta_description(town, upcoming=437, window=window, window_count=12)
    assert len(text) <= seo.META_DESCRIPTION_LIMIT
    assert "Atlanta, Georgia" in text
    assert "Spelman College" not in text  # only the first schools are named


def test_descriptions_differ_between_towns(make_city):
    a = make_city(name="Ithaca", slug="ithaca-ny", state_or_region="NY",
                  universities=["Cornell University", "Ithaca College"])
    b = make_city(name="Hanover", slug="hanover-nh", state_or_region="NH",
                  universities=["Dartmouth College"])
    da = seo.town_meta_description(a, upcoming=386)
    db = seo.town_meta_description(b, upcoming=326)
    assert da != db
    assert "Cornell University and Ithaca College" in da
    assert len(da) <= seo.META_DESCRIPTION_LIMIT


def test_a_town_page_names_and_links_its_sources(client, town_with_events):
    town_with_events(positions=BLOOMINGTON[:1])
    html = client.get("/city/bloomington-in").text
    assert "Where Bloomington's listings come from" in html
    # The source's site, never the feed address its events are read from.
    assert 'href="https://events.iu.edu/"' in html
    assert "feed.json" not in html
    assert ">IU Calendar<span" in html


def test_town_page_canonical_and_robots_are_unchanged(client, town_with_events, monkeypatch):
    monkeypatch.setattr(seo, "MIN_INDEXABLE_TOWN_EVENTS", 1)
    town_with_events(positions=BLOOMINGTON)
    html = client.get("/city/bloomington-in?category_id=3").text
    assert re.search(r'<link rel="canonical" href="[^"]*/city/bloomington-in" />', html)
    assert '<meta name="robots" content="index, follow" />' in html


def test_site_root_keeps_only_the_site():
    from app.core.formatting import site_root

    assert site_root("https://user:pw@cal.example.edu:8443/api?x=1") == "https://cal.example.edu:8443/"
    assert site_root("javascript:alert(1)") == ""
    assert site_root(None) == ""
