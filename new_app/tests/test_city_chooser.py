"""The home page chooses a college town; it never lists every city's events.

This product covers college towns, where "what is on" only means something once
a town is chosen. A feed mixing Bloomington and Bethlehem is noise to a reader
who lives in one of them, so no route serves one.
"""

import re
from datetime import timedelta

import pytest

from app.repositories.public_events import current_public_date


@pytest.fixture
def college_towns(db_session, make_city, make_website, make_event):
    def _publish(city, title):
        website = make_website(city, name=f"{city.name} Source")
        website.is_active = True
        website.active_configuration_version = 1
        db_session.commit()
        make_event(
            city,
            website=website,
            title=title,
            canonical_url=f"https://example.org/{title.lower().replace(' ', '-')}",
            start_date=current_public_date() + timedelta(days=3),
        )
        db_session.commit()

    bloomington = make_city(
        name="Bloomington", slug="bloomington-in",
        state_or_region="Indiana", university_name="Indiana University",
    )
    bethlehem = make_city(
        name="Bethlehem", slug="bethlehem-pa",
        state_or_region="Pennsylvania", university_name="Lehigh University",
    )
    _publish(bloomington, "Hoosier Concert")
    _publish(bethlehem, "Lehigh Lecture")
    return bloomington, bethlehem


def test_the_home_page_asks_for_a_town(client, college_towns):
    html = client.get("/").text
    assert "What college town?" in html
    assert 'name="q"' in html


def test_the_home_page_is_not_an_events_listing(client, college_towns):
    """The point of the change: no page mixes two towns' events."""
    html = client.get("/").text
    assert "Hoosier Concert" not in html
    assert "Lehigh Lecture" not in html


def test_both_towns_are_offered_with_their_school(client, college_towns):
    html = client.get("/").text
    assert "Indiana University" in html
    assert "Lehigh University" in html


def test_a_town_shows_only_its_own_events(client, college_towns):
    bloomington, _ = college_towns
    html = client.get(f"/city/{bloomington.slug}").text
    assert "Hoosier Concert" in html
    assert "Lehigh Lecture" not in html


@pytest.mark.parametrize(
    "term,expected,absent",
    [
        ("lehigh", "Bethlehem", "Bloomington"),      # by school
        ("bloom", "Bloomington", "Bethlehem"),       # by town
        ("Indiana", "Bloomington", "Bethlehem"),     # by state, and by school
    ],
)
def test_search_matches_town_school_and_state(client, college_towns, term, expected, absent):
    """A visitor is as likely to think "Lehigh" as "Bethlehem"."""
    html = client.get(f"/?q={term}").text
    # Scoped to the results: the search box echoes the term, so matching the
    # whole page would pass on the input's own value.
    results = re.search(r'<ul class="city-grid">(.*?)</ul>', html, re.S)
    assert results, "no results list rendered"
    assert expected in results.group(1)
    assert absent not in results.group(1)


def test_an_unmatched_search_says_so_and_offers_a_way_back(client, college_towns):
    html = client.get("/?q=nowhere").text
    assert "No college town matches" in html
    assert 'href="/"' in html


def test_the_town_page_cannot_be_widened_to_every_city(client, college_towns):
    """The city filter is pinned by the route, so a crafted city_id cannot turn
    a town page into a cross-city feed."""
    bloomington, bethlehem = college_towns
    html = client.get(f"/city/{bloomington.slug}?city_id={bethlehem.id}").text
    assert "Hoosier Concert" in html
    assert "Lehigh Lecture" not in html


def test_the_town_filter_bar_offers_no_all_cities_option(client, college_towns):
    bloomington, _ = college_towns
    html = client.get(f"/city/{bloomington.slug}").text
    assert "All cities" not in html


def test_a_town_page_titles_itself_for_search(client, college_towns):
    """Every town used to share one title and one description."""
    bloomington, bethlehem = college_towns
    one = client.get(f"/city/{bloomington.slug}").text
    two = client.get(f"/city/{bethlehem.slug}").text

    def meta(html, name):
        return re.search(rf'<meta name="{name}" content="([^"]*)"', html).group(1)

    assert "<title>Events in Bloomington — Indiana University</title>" in one
    assert meta(one, "description") != meta(two, "description")
    assert "Indiana University" in meta(one, "description")


def test_an_inactive_town_is_not_offered_or_reachable(client, college_towns, db_session):
    _, bethlehem = college_towns
    bethlehem.is_active = False
    db_session.commit()

    assert "Bethlehem" not in client.get("/").text
    assert client.get(f"/city/{bethlehem.slug}").status_code == 404


def test_counts_match_what_the_town_page_would_show(client, college_towns):
    """A count that promises events the town page then hides would be worse
    than no count at all."""
    bloomington, _ = college_towns
    chooser = client.get("/").text
    card = re.search(
        r"Bloomington.*?(\d+) upcoming event", chooser, re.S
    ).group(1)
    assert card == "1"
