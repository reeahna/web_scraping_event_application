"""Event structured data against Google's Event requirements.

Google requires name, startDate, location and location.address on every event,
and asks for start and end times with their UTC offset. Production pages
lacked an address on most events, had no offsets, and named the scraped
source as the organizer. Nothing here may be invented: no venue, organizer,
image or price the source did not give.
"""

import json
import re
from datetime import date, time, timedelta

from app.repositories.public_events import current_public_date
from app.services import seo


def _event_data(html: str) -> dict:
    for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        data = json.loads(block)
        if data.get("@type") == "Event":
            return data
    raise AssertionError("no Event JSON-LD on the page")


def _town(make_city, **values):
    defaults = dict(
        name="Ann Arbor", slug="ann-arbor-mi", state_or_region="MI",
        timezone="America/Detroit",
    )
    defaults.update(values)
    return make_city(**defaults)


def test_every_event_has_a_location_with_an_address(db_session, make_city, make_event):
    """No venue and no address from the source: the town is the location,
    as Google's guidelines ask, and no venue is made up."""
    city = _town(make_city)
    event = make_event(city, title="Open Studio", canonical_url="https://example.org/a")
    db_session.commit()

    place = seo.event_schema(event)["location"]
    assert place["@type"] == "Place"
    assert place["name"] == "Ann Arbor, Michigan"
    assert place["address"] == {
        "@type": "PostalAddress",
        "addressLocality": "Ann Arbor",
        "addressRegion": "Michigan",
        "addressCountry": "US",
    }


def test_a_known_venue_and_street_are_used_as_given(db_session, make_city, make_event):
    city = _town(make_city)
    event = make_event(
        city, title="Jazz", canonical_url="https://example.org/j",
        venue="The Ark", address="316 S Main St",
    )
    db_session.commit()

    place = seo.event_schema(event)["location"]
    assert place["name"] == "The Ark"
    assert place["address"]["streetAddress"] == "316 S Main St"
    assert place["address"]["addressLocality"] == "Ann Arbor"


def test_an_admin_correction_wins_over_the_scraped_venue(db_session, make_city, make_event):
    city = _town(make_city)
    event = make_event(city, title="Talk", canonical_url="https://example.org/t", venue="TBD")
    event.corrected_venue = "Rackham Auditorium"
    db_session.commit()
    assert seo.event_schema(event)["location"]["name"] == "Rackham Auditorium"


def test_a_venue_without_a_street_still_carries_the_town_address(
    db_session, make_city, make_event
):
    city = _town(make_city)
    event = make_event(city, title="Gallery", canonical_url="https://example.org/g", venue="UMMA")
    db_session.commit()
    place = seo.event_schema(event)["location"]
    assert place["name"] == "UMMA"
    assert "streetAddress" not in place["address"]
    assert place["address"]["addressRegion"] == "Michigan"


def test_a_non_us_region_is_not_labelled_us(db_session, make_city, make_event):
    city = make_city(name="Waterloo", slug="waterloo-on", state_or_region="Ontario")
    event = make_event(city, title="Show", canonical_url="https://example.org/w")
    db_session.commit()
    address = seo.event_schema(event)["location"]["address"]
    assert address["addressRegion"] == "Ontario"
    assert "addressCountry" not in address


def test_times_carry_the_utc_offset_for_their_own_date(db_session, make_city, make_event):
    """Detroit is UTC-4 in October and UTC-5 in December: the offset follows
    daylight saving for each date rather than one fixed value."""
    city = _town(make_city)
    summer = make_event(
        city, title="Before DST ends", canonical_url="https://example.org/s",
        start_date=date(2026, 10, 3), start_time=time(19, 30),
        end_date=date(2026, 10, 3), end_time=time(22, 0),
    )
    winter = make_event(
        city, title="After DST ends", canonical_url="https://example.org/w2",
        start_date=date(2026, 12, 6), start_time=time(14, 0),
    )
    db_session.commit()

    data = seo.event_schema(summer)
    assert data["startDate"] == "2026-10-03T19:30:00-04:00"
    assert data["endDate"] == "2026-10-03T22:00:00-04:00"
    assert seo.event_schema(winter)["startDate"] == "2026-12-06T14:00:00-05:00"


def test_the_event_s_own_zone_wins_over_its_town_s(db_session, make_city, make_event):
    city = _town(make_city)
    event = make_event(
        city, title="Webcast", canonical_url="https://example.org/c",
        start_date=date(2026, 12, 6), start_time=time(14, 0),
    )
    event.timezone = "America/Chicago"
    db_session.commit()
    assert seo.event_schema(event)["startDate"] == "2026-12-06T14:00:00-06:00"


def test_a_bare_us_abbreviation_still_observes_daylight_saving(
    db_session, make_city, make_event
):
    city = _town(make_city, timezone="EST")
    event = make_event(
        city, title="July", canonical_url="https://example.org/jul",
        start_date=date(2026, 7, 4), start_time=time(21, 0),
    )
    db_session.commit()
    assert seo.event_schema(event)["startDate"] == "2026-07-04T21:00:00-04:00"


def test_no_offset_is_guessed_when_no_zone_is_set(db_session, make_city, make_event):
    city = _town(make_city, timezone="UTC")
    event = make_event(
        city, title="Unknown zone", canonical_url="https://example.org/u",
        start_date=date(2026, 12, 6), start_time=time(14, 0),
    )
    db_session.commit()
    assert seo.event_schema(event)["startDate"] == "2026-12-06T14:00:00"


def test_all_day_events_stay_plain_dates(db_session, make_city, make_event):
    city = _town(make_city)
    event = make_event(
        city, title="Fair", canonical_url="https://example.org/f",
        start_date=date(2026, 12, 6), end_date=date(2026, 12, 8),
    )
    db_session.commit()
    data = seo.event_schema(event)
    assert data["startDate"] == "2026-12-06"
    assert data["endDate"] == "2026-12-08"


def test_the_source_is_not_presented_as_the_organizer(db_session, make_city, make_event):
    city = _town(make_city)
    event = make_event(city, title="GBM", canonical_url="https://example.org/o")
    event.source = "- TerpLink"
    db_session.commit()
    data = seo.event_schema(event)
    assert "organizer" not in data
    assert "TerpLink" not in json.dumps(data)


def test_nothing_is_invented_when_the_source_gives_little(db_session, make_city, make_event):
    city = _town(make_city)
    event = make_event(city, title="Bare", canonical_url="https://example.org/b")
    db_session.commit()
    data = seo.event_schema(event)
    for key in ("image", "offers", "organizer", "performer", "endDate", "description"):
        assert key not in data


def test_the_town_listing_uses_the_same_event_data(db_session, make_city, make_event):
    """Town pages embed each event; they get the same address and offsets."""
    city = _town(make_city)
    event = make_event(
        city, title="Listed", canonical_url="https://example.org/l",
        start_date=date(2026, 12, 6), start_time=time(14, 0),
    )
    db_session.commit()
    listing = json.loads(
        seo.city_listing_structured_data(city, [event], page_url="https://x/city/ann-arbor-mi")
    )
    item = listing["itemListElement"][0]["item"]
    assert item["location"]["address"]["addressLocality"] == "Ann Arbor"
    assert item["startDate"].endswith("-05:00")


def test_event_page_renders_required_fields_and_machine_readable_times(
    client, db_session, make_city, make_website, make_event
):
    city = _town(make_city)
    website = make_website(city, name="UMS")
    website.is_active = True
    website.active_configuration_version = 1
    db_session.commit()
    day = current_public_date() + timedelta(days=5)
    event = make_event(
        city, website=website, title="Symphony", canonical_url="https://example.org/sym",
        start_date=day, start_time=time(19, 30), end_time=time(21, 0), venue="Hill Auditorium",
    )
    db_session.commit()

    html = client.get(f"/events/{event.id}").text
    data = _event_data(html)
    for key in ("name", "startDate", "location"):
        assert data[key]
    assert data["location"]["address"]["addressLocality"] == "Ann Arbor"
    assert re.search(r"[+-]\d\d:\d\d$", data["startDate"])

    # The same values a reader sees are in <time datetime> attributes.
    assert f'<time datetime="{day.isoformat()}">' in html
    assert f'<time datetime="{data["startDate"]}">' in html
    assert f'<time datetime="{data["endDate"]}">' in html
