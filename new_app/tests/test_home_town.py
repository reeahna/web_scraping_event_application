"""Keeping each source to the town it was added for."""

import json

import httpx
import pytest

from app.config import get_settings
from app.models.event import Event
from app.models.extraction_run import ExtractionRun
from app.schemas.extraction import SiteConfiguration
from app.services.extraction_runs import preview_extraction, run_extraction
from app.services.home_town import address_town, hide_out_of_town_events, is_outside_home_town
from app.services.website_configuration import approve_configuration
from tests.extraction_helpers import html_handler, patched_http_fetch


@pytest.fixture(autouse=True)
def _home_town_filter_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "home_town_filter_enabled", True)


@pytest.mark.parametrize(
    "address,expected",
    [
        ("849 South Auto Mall Road, Bloomington, IN, 47401", ("Bloomington", "IN")),
        ("30 Library Dr., Bethlehem, PA 18015", ("Bethlehem", "PA")),
        ("Indiana Memorial Union, 900 E 7th St, Bloomington, Indiana", ("Bloomington", "IN")),
        ("Waldron Arts Center 122 S. Walnut Street, Bloomington, IN", ("Bloomington", "IN")),
        ("Zoellner Arts Center, Bethlehem, PA, USA", ("Bethlehem", "PA")),
        ("Bloomington", None),
        ("205 Locust Lane, Nashville", None),
        ("Assembly Hall", None),
        (None, None),
    ],
)
def test_address_town(address, expected):
    assert address_town(address) == expected


@pytest.mark.parametrize(
    "address,outside",
    [
        ("1413 East 17th Street, Bloomington, IN, 47408", False),
        ("123 Main St, Bloominton, IN", False),  # a typo is still the town
        ("1 Main St Bloomington, IN 47401", False),  # no comma before the town
        ("10 W Market St, Indianapolis, IN, 46204", True),
        ("701 Wabash Ave, Terre Haute, IN, 47807", True),
        ("100 N Center St, Bloomington, IL 61701", True),  # same name, other state
        ("Assembly Hall", False),  # no town named: kept
        (None, False),
    ],
)
def test_is_outside_home_town(address, outside):
    assert (
        is_outside_home_town(address=address, venue=None, town="Bloomington", state="Indiana")
        is outside
    )


# --- the import -------------------------------------------------------------

JSONLD_CONFIG = SiteConfiguration(
    pattern_name="json_ld_event", listing_url="https://example.com/events"
)


def _event(name: str, slug: str, town: str) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "Event",
        "name": name,
        "startDate": "2030-06-01T19:00:00-05:00",
        "url": f"https://example.com/events/{slug}",
        "location": {
            "@type": "Place",
            "name": "The Venue",
            "address": {
                "@type": "PostalAddress",
                "streetAddress": "1 Main Street",
                "addressLocality": town,
                "addressRegion": "IN",
                "postalCode": "47401",
            },
        },
    }


def _handler(*events: dict):
    blocks = "".join(
        f'<script type="application/ld+json">{json.dumps(e)}</script>' for e in events
    )
    body = f"<html><head><title>Events</title>{blocks}</head><body></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    return handler


async def _approved(db_session, make_city, make_website, make_user, config=JSONLD_CONFIG):
    city = make_city(name="Bloomington", slug="bloomington", state_or_region="Indiana")
    website = make_website(city, name="Eventbrite", base_url="https://example.com")
    admin = make_user(email="approver@example.com")
    website.configuration = config.model_dump(mode="json")
    db_session.commit()
    with patched_http_fetch(html_handler("jsonld_single_event.html")):
        await preview_extraction(db_session, website)
    approve_configuration(db_session, website, approved_by_user_id=admin.id)
    return website


@pytest.mark.asyncio
async def test_import_leaves_out_events_in_other_towns(
    db_session, make_city, make_website, make_user
):
    website = await _approved(db_session, make_city, make_website, make_user)
    handler = _handler(
        _event("Jazz Night", "jazz", "Bloomington"),
        _event("Indy Brewfest", "brew", "Indianapolis"),
        _event("Terre Haute Fair", "fair", "Terre Haute"),
    )
    with patched_http_fetch(handler):
        result = await run_extraction(db_session, website, triggered_by_user_id=None)

    assert [e.title for e in db_session.query(Event)] == ["Jazz Night"]
    run = db_session.get(ExtractionRun, result.run_id)
    assert "outside_home_town_excluded:2" in run.warnings


@pytest.mark.asyncio
async def test_import_takes_down_out_of_town_copies_imported_before(
    db_session, make_city, make_website, make_user, monkeypatch
):
    website = await _approved(db_session, make_city, make_website, make_user)
    handler = _handler(
        _event("Jazz Night", "jazz", "Bloomington"),
        _event("Indy Brewfest", "brew", "Indianapolis"),
    )
    monkeypatch.setattr(get_settings(), "home_town_filter_enabled", False)
    with patched_http_fetch(handler):
        await run_extraction(db_session, website, triggered_by_user_id=None)
    assert db_session.query(Event).filter_by(is_active=True).count() == 2

    monkeypatch.setattr(get_settings(), "home_town_filter_enabled", True)
    with patched_http_fetch(handler):
        await run_extraction(db_session, website, triggered_by_user_id=None)
    db_session.expire_all()
    assert [e.title for e in db_session.query(Event).filter_by(is_active=True)] == ["Jazz Night"]


@pytest.mark.asyncio
async def test_a_source_with_its_own_geographic_filter_is_left_to_it(
    db_session, make_city, make_website, make_user
):
    config = JSONLD_CONFIG.model_copy(
        update={
            "geographic_filters": {"localities": ["Bloomington", "Indianapolis"]},
        }
    )
    config = SiteConfiguration.model_validate(config.model_dump())
    website = await _approved(db_session, make_city, make_website, make_user, config=config)
    handler = _handler(
        _event("Jazz Night", "jazz", "Bloomington"),
        _event("Indy Brewfest", "brew", "Indianapolis"),
    )
    with patched_http_fetch(handler):
        await run_extraction(db_session, website, triggered_by_user_id=None)

    assert sorted(e.title for e in db_session.query(Event)) == ["Indy Brewfest", "Jazz Night"]


def test_cleanup_dry_run_changes_nothing_then_apply_deactivates(
    db_session, make_city, make_event
):
    city = make_city(name="Bloomington", slug="bloomington", state_or_region="Indiana")
    make_event(city, title="Jazz Night", canonical_url="https://x/1",
               address="1 Main St, Bloomington, IN 47401")
    make_event(city, title="Indy Brewfest", canonical_url="https://x/2",
               address="1 Main St, Indianapolis, IN 46204")
    make_event(city, title="Campus Talk", canonical_url="https://x/3", venue="Assembly Hall")

    found = hide_out_of_town_events(db_session, apply=False)
    assert [e.title for e in found] == ["Indy Brewfest"]
    assert db_session.query(Event).filter_by(is_active=True).count() == 3

    hide_out_of_town_events(db_session, apply=True)
    db_session.expire_all()
    active = sorted(e.title for e in db_session.query(Event).filter_by(is_active=True))
    assert active == ["Campus Talk", "Jazz Night"]
