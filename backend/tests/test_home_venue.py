"""A source's administrator-set home venue (SiteConfiguration.home_venue_name).

It stands in only for a venue the event itself never names. An extracted
venue, a venue the page lookup found, an earlier stored venue and an
administrator's location correction all take precedence, and the default is
recorded as source-level provenance. Nothing sets it automatically.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.extraction.normalize import (
    HOME_VENUE_MARKER,
    HOME_VENUE_SOURCE_PATH,
    normalize_candidate,
)
from app.extraction.types import EventCandidate
from app.repositories.event import create_event_from_candidate, update_event
from app.schemas.extraction import SiteConfiguration
from app.schemas.extraction_config_forms import (
    ConfigFormInput,
    build_site_configuration,
    configuration_to_form,
)

URL = "https://museum.example.org/events"
HOME = "Speed Art Museum"
HOME_ADDRESS = "2035 S Third St, Louisville, KY 40208"


def _config(**overrides) -> SiteConfiguration:
    values = {
        "pattern_name": "the_events_calendar",
        "api_endpoint": URL,
        "timezone": "America/Kentucky/Louisville",
        "home_venue_name": HOME,
        "home_venue_address": HOME_ADDRESS,
    }
    values.update(overrides)
    return SiteConfiguration(**values)


def _candidate(venue=None, address=None, slug="tour") -> EventCandidate:
    raw = {
        "title": "Collection Highlights Tour",
        "canonical_url": f"https://museum.example.org/event/{slug}/",
        "start_datetime": "2026-11-28T14:00:00",
        "venue": venue,
        "address": address,
    }
    paths = {"title": "events[0].title"}
    if venue:
        paths["venue"] = "events[0].venue.venue"
    return EventCandidate(
        raw=raw, title=None, canonical_url=None, description=None, start_date=None,
        start_time=None, end_date=None, end_time=None, timezone=None, venue=None,
        address=None, image_url=None, latitude=None, longitude=None,
        source_category=None, external_source_id=None, field_source_paths=paths,
        transformation_history=(), source_page=URL, extraction_pattern="the_events_calendar",
        warnings=(), raw_record_hash=f"hash-{slug}",
    )


# --- normalizing ---------------------------------------------------------------


def test_home_venue_fills_an_event_with_no_venue():
    event = normalize_candidate(_candidate(), _config())
    assert event.venue == HOME
    assert event.address == HOME_ADDRESS
    assert event.field_source_paths["venue"] == HOME_VENUE_SOURCE_PATH
    assert event.field_source_paths["address"] == HOME_VENUE_SOURCE_PATH
    assert HOME_VENUE_MARKER in event.transformation_history


def test_an_extracted_venue_always_wins():
    event = normalize_candidate(_candidate(venue="Speed Cinema"), _config())
    assert event.venue == "Speed Cinema"
    # The home address is not attached to a venue the event named itself.
    assert event.address is None
    assert event.field_source_paths["venue"] == "events[0].venue.venue"
    assert HOME_VENUE_MARKER not in event.transformation_history


def test_a_placeholder_venue_counts_as_missing():
    event = normalize_candidate(_candidate(venue="TBA"), _config())
    assert event.venue == HOME


def test_the_events_own_address_is_kept():
    event = normalize_candidate(_candidate(address="Main lobby entrance"), _config())
    assert event.venue == HOME
    assert event.address == "Main lobby entrance"
    assert "address" not in event.field_source_paths


def test_no_home_venue_means_no_change():
    event = normalize_candidate(
        _candidate(), _config(home_venue_name=None, home_venue_address=None)
    )
    assert event.venue is None and event.address is None
    assert HOME_VENUE_MARKER not in event.transformation_history


def test_name_without_an_address_fills_only_the_name():
    event = normalize_candidate(_candidate(), _config(home_venue_address=None))
    assert (event.venue, event.address) == (HOME, None)


# --- validation and the configure form ------------------------------------------


def test_an_address_without_a_name_is_rejected():
    with pytest.raises(ValidationError):
        _config(home_venue_name="  ", home_venue_address=HOME_ADDRESS)


def test_home_venue_text_is_trimmed_and_bounded():
    assert _config(home_venue_name="  Speed   Art Museum ").home_venue_name == HOME
    with pytest.raises(ValidationError):
        _config(home_venue_name="x" * 201)


def test_home_venue_is_off_by_default():
    config = SiteConfiguration(pattern_name="the_events_calendar", api_endpoint=URL)
    assert config.home_venue_name is None and config.home_venue_address is None


def test_configure_form_round_trip():
    config = _config()
    form = configuration_to_form(config)
    assert (form.home_venue_name, form.home_venue_address) == (HOME, HOME_ADDRESS)
    rebuilt = build_site_configuration(form, base=config)
    assert rebuilt.configuration.home_venue_name == HOME
    assert rebuilt.configuration.home_venue_address == HOME_ADDRESS


def test_blank_form_fields_clear_the_home_venue():
    form = configuration_to_form(_config())
    form.home_venue_name = ""
    form.home_venue_address = ""
    rebuilt = build_site_configuration(form, base=_config())
    assert rebuilt.configuration.home_venue_name is None
    assert rebuilt.configuration.home_venue_address is None


def test_form_input_defaults_to_blank():
    form = ConfigFormInput(pattern_name="the_events_calendar")
    assert form.home_venue_name == "" and form.home_venue_address == ""


# --- storing ------------------------------------------------------------------


@pytest.fixture
def museum(db_session, make_city, make_website):
    city = make_city(name="Louisville", slug="louisville-ky", state_or_region="KY")
    website = make_website(city, name="Speed Art Museum")
    return city, website


def test_import_stores_the_home_venue_for_a_new_event(db_session, museum):
    city, website = museum
    candidate = normalize_candidate(_candidate(), _config())
    event = create_event_from_candidate(
        db_session, candidate, source="Speed Art Museum", city_id=city.id, website_id=website.id
    )
    assert (event.venue, event.address) == (HOME, HOME_ADDRESS)


def test_reimport_never_replaces_a_stored_venue_with_the_home_venue(
    db_session, museum, make_event
):
    city, website = museum
    event = make_event(
        city, website=website, title="Collection Highlights Tour",
        canonical_url="https://museum.example.org/event/tour/",
        start_date=date(2026, 11, 28), venue="Speed Cinema", address="Lower level",
    )
    update_event(db_session, event, normalize_candidate(_candidate(), _config()))
    assert (event.venue, event.address) == ("Speed Cinema", "Lower level")


def test_reimport_fills_a_stored_event_that_had_no_venue(db_session, museum, make_event):
    city, website = museum
    event = make_event(
        city, website=website, title="Collection Highlights Tour",
        canonical_url="https://museum.example.org/event/tour/",
        start_date=date(2026, 11, 28), venue=None,
    )
    update_event(db_session, event, normalize_candidate(_candidate(), _config()))
    assert (event.venue, event.address) == (HOME, HOME_ADDRESS)


def test_reimport_with_a_real_venue_still_updates_a_stored_one(db_session, museum, make_event):
    city, website = museum
    event = make_event(
        city, website=website, title="Collection Highlights Tour",
        canonical_url="https://museum.example.org/event/tour/",
        start_date=date(2026, 11, 28), venue="Old Gallery",
    )
    update_event(db_session, event, normalize_candidate(_candidate(venue="New Wing"), _config()))
    assert event.venue == "New Wing"


def test_an_administrator_correction_stays_the_public_venue(db_session, museum, make_event):
    city, website = museum
    event = make_event(
        city, website=website, title="Collection Highlights Tour",
        canonical_url="https://museum.example.org/event/tour/",
        start_date=date(2026, 11, 28), venue=None, corrected_venue="Speed Art Museum Atrium",
    )
    update_event(db_session, event, normalize_candidate(_candidate(), _config()))
    assert event.venue == HOME
    assert event.corrected_venue == "Speed Art Museum Atrium"
    assert event.public_venue == "Speed Art Museum Atrium"
