"""Requests a visitor (or a bot) can make by hand: absurd ids and page numbers,
search text full of wildcard characters. None of them may cause a 500."""

from datetime import timedelta

import pytest

from app.repositories.city import search_public_cities
from app.repositories.public_events import current_public_date

HTML = {"Accept": "text/html"}
HUGE = "99999999999999999999"


@pytest.fixture
def town_event(make_city, make_website, make_event):
    town = make_city(name="Bloomington", slug="bloomington")
    site = make_website(town, is_active=True, approved_pattern={"pattern_name": "static_html"})
    event = make_event(town, website=site, title="Jazz Night",
                       start_date=current_public_date() + timedelta(days=3))
    return town, event


@pytest.mark.parametrize(
    "path",
    [
        f"/events/{HUGE}",
        f"/city/bloomington?page={HUGE}",
        f"/city/bloomington?category_id={HUGE}",
        f"/city/bloomington?exclude_source={HUGE}",
        f"/events/map?city_id={HUGE}",
    ],
)
def test_huge_numbers_never_cause_a_server_error(client, town_event, path):
    assert client.get(path, headers=HTML).status_code < 500


def test_an_out_of_range_event_id_is_not_found(client, town_event):
    resp = client.get(f"/events/{HUGE}", headers=HTML)
    assert resp.status_code == 404
    assert "Page not found" in resp.text


def test_a_non_numeric_event_id_gets_the_not_found_page_not_json(client, town_event):
    resp = client.get("/events/abc", headers=HTML)
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("text/html")
    # API callers still get the field-level detail.
    assert client.get("/events/abc").status_code == 422


@pytest.mark.parametrize("term", ["%", "_", "%%", "\\"])
def test_wildcard_characters_in_search_are_taken_literally(client, town_event, term):
    html = client.get("/city/bloomington", params={"q": term}).text
    assert "Jazz Night" not in html


def test_search_still_finds_a_plain_term(client, town_event):
    html = client.get("/city/bloomington", params={"q": "jazz"}).text
    assert "Jazz Night" in html


def test_town_search_treats_percent_literally(db_session, town_event):
    assert search_public_cities(db_session, query="%") == []
    assert [c.name for c in search_public_cities(db_session, query="bloom")] == ["Bloomington"]
