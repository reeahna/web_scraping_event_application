"""Keeping each source to the town it was added for."""

import json

import httpx
import pytest

from app.config import get_settings
from app.models.event import Event
from app.models.extraction_run import ExtractionRun
from app.schemas.extraction import SiteConfiguration
from app.services.extraction_runs import preview_extraction, run_extraction
from app.services.home_town import (
    address_town,
    from_other_campus_calendar,
    hide_out_of_town_events,
    is_away_game,
    is_outside_home_town,
    names_other_campus,
)
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
        ("205 Locust Lane, Nashville", ("Nashville", None)),
        ("Lawrence County Courthouse Square 1005 15th St, Bedford", ("Bedford", None)),
        ("Monroe County Publc Library, 303 E. Kirkwood Ave., Bloomington", ("Bloomington", None)),
        ("Waldron Arts Center, Rose Firebay", None),  # no street before it
        ("12 Main St, Kirkwood Ave", None),  # a street, not a town
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
        ("Lawrence County Courthouse Square 1005 15th St, Bedford", True),
        ("303 E. Kirkwood Ave., Bloomington", False),
        ("Assembly Hall", False),  # no town named: kept
        ("1140 IN-135, Greenwood, IN, 46143", True),
        (None, False),
    ],
)
def test_is_outside_home_town(address, outside):
    assert (
        is_outside_home_town(address=address, venue=None, town="Bloomington", state="Indiana")
        is outside
    )


@pytest.mark.parametrize(
    "address,outside",
    [
        ("120 N Dubuque St, Iowa City, IA, 52245", False),
        ("1100 3rd St SE, Cedar Rapids, IA, 52401", True),
    ],
)
def test_is_outside_home_town_for_any_town(address, outside):
    assert (
        is_outside_home_town(address=address, venue=None, town="Iowa City", state="Iowa")
        is outside
    )


@pytest.mark.parametrize(
    "url,other",
    [
        ("https://events.iu.edu/indianapolis/event/1-llsa-gb", True),
        ("https://events.iu.edu/studentaffairs-indianapolis/event/2-mat-pilates", True),
        ("https://events.iu.edu/southbend/event/3-cpr", True),
        ("https://events.iu.edu/iunstuactivities/event/4-redhawk-welcome", True),
        ("https://events.iu.edu/east/event/5-follow-the-pack", True),
        ("https://events.iu.edu/campus-center/event/6-imu-recruitment", False),
        ("https://events.iu.edu/academicdatesiub/event/7-refund", False),
        ("https://events.iu.edu/live/event/8-back-to-school-bash", False),
        ("https://example.com/indianapolis/event/9", False),  # only campus calendars
        (None, False),
    ],
)
def test_other_campus_calendar(url, other):
    assert from_other_campus_calendar(url) is other


@pytest.mark.parametrize(
    "title,other",
    [
        ("IU Indy Garden Volunteer Hours", True),
        ("9/4 Indiana University Southeast Men’s Golf", False),  # caught by its calendar
        ("IU Southeast Open House", True),
        ("IUS Grenadier Welcome", True),
        ("IU Auditorium: Wicked", False),
        ("Indiana Daily Student Open House", False),
    ],
)
def test_other_campus_title_on_a_shared_calendar(title, other):
    url = "https://events.iu.edu/live/event/1-x"
    assert from_other_campus_calendar(url, title) is other
    assert from_other_campus_calendar("https://example.com/live/event/1-x", title) is False


@pytest.mark.parametrize(
    "title,away",
    [
        ("10/9 Indiana University Women’s Rowing at Iowa (Scrimmage)", True),
        ("9/6 1:00 PM Indiana University Women's Soccer at Ohio", True),
        ("Lehigh University Football at Georgetown", True),
        ("Lehigh University Women's Golf at Charge At Quail Valley", True),
        ("9/6 3:00 PM Indiana University Women's Volleyball vs #22 Tennessee", False),
        ("IU Football Watch Party: IU at Purdue", False),
        ("International Night at IU Men's Soccer", False),
        ("Paul Page Memorial Golf Outing 2026 at Hickory Stick", False),
        ("Lions Club 2026 Golf Tournament at Arrowhead Golf Course", False),
        # A tournament named for another host is away; the school's own is not.
        (
            "10/12 Indiana University Men\u2019s Golf Purdue Fall Invitational"
            " - Kampen-Cosler Golf Course",
            True,
        ),
        (
            "10/12 Indiana University Women\u2019s Golf vs Illini Women\u2019s Invitational"
            " at Medinah - Medinah Country Club",
            True,
        ),
        ("Indiana University Baseball Round Rock Classic", True),
        ("Indiana University Men's Golf Indiana Invitational", False),
        ("IU Women's Golf IU Fall Classic", False),
        ("Indiana University Men's Golf Hoosier Invitational - IU Golf Course", False),
        ("Indiana University Women's Tennis Bloomington Shootout", False),  # names the town
        ("Indiana University Football vs Purdue Old Oaken Bucket Classic", False),
        ("Lions Club Golf Classic", False),
        ("State Street Golf Classic", False),
        ("IU Swimming Watch Party: Purdue Invitational", False),
        (None, False),
    ],
)
def test_is_away_game(title, away):
    assert is_away_game(title, "Bloomington") is away


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
    make_event(city, title="IU Indy Volleyball",
               canonical_url="https://events.iu.edu/athletics-indianapolis/event/1-vb")
    make_event(city, title="10/12 Indiana University Men's Golf Purdue Fall Invitational",
               canonical_url="https://events.iu.edu/live/event/2-golf")

    found = hide_out_of_town_events(db_session, apply=False)
    assert [e.title for e in found] == [
        "Indy Brewfest",
        "IU Indy Volleyball",
        "10/12 Indiana University Men's Golf Purdue Fall Invitational",
    ]
    assert db_session.query(Event).filter_by(is_active=True).count() == 5

    hide_out_of_town_events(db_session, apply=True)
    db_session.expire_all()
    active = sorted(e.title for e in db_session.query(Event).filter_by(is_active=True))
    assert active == ["Campus Talk", "Jazz Night"]


@pytest.mark.asyncio
async def test_scheduler_sweep_takes_down_out_of_town_events(db_session, make_city, make_event):
    from app.scheduler.runtime import SchedulerRuntime

    city = make_city(name="Bloomington", slug="bloomington", state_or_region="Indiana")
    make_event(city, title="Jazz Night", canonical_url="https://x/1",
               address="1 Main St, Bloomington, IN 47401")
    make_event(city, title="Paint and Sip", canonical_url="https://x/2",
               address="1140 IN-135, Greenwood, IN, 46143")

    runtime = SchedulerRuntime(holder="test")
    await runtime._home_town_sweep_tick()  # not the leader: leaves everything
    db_session.expire_all()
    assert db_session.query(Event).filter_by(is_active=True).count() == 2

    runtime._is_leader = True
    await runtime._home_town_sweep_tick()
    db_session.expire_all()
    assert [e.title for e in db_session.query(Event).filter_by(is_active=True)] == ["Jazz Night"]


@pytest.mark.parametrize(
    "title,town,away",
    [
        ("Iowa Ice Hockey at Nebraska", "Iowa City", True),
        ("Iowa Ice Hockey at SIUE", "Iowa City", True),
        ("Iowa Volleyball vs. USC", "Iowa City", False),
        ("Pickup Basketball at SRSC", "Bloomington", False),
        ("Pickup Basketball at the Rec", "Iowa City", False),
    ],
)
def test_town_team_away_game(title, town, away):
    assert is_away_game(title, town) is away


@pytest.mark.parametrize(
    "title,other",
    [
        ("10/17 1:00 PM Indiana University Kokomo Volleyball vs Kentucky Christian", True),
        ("10/17 4:00 PM IU Indy Men’s Soccer vs Milwaukee", True),
        ("IUK Women’s Volleyball 10/17/26", True),
        ("10/16 7:30 PM Indiana University Women’s Soccer vs #5 Michigan State", False),
        ("IU East Asian Studies Center Film Night", False),
    ],
)
def test_title_naming_another_campus(title, other):
    assert names_other_campus(title, "Bloomington") is other
    assert names_other_campus(title, "Iowa City") is False
