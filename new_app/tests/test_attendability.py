"""Leaving out what is not an event a person can go to."""

import json

import httpx
import pytest

from app.config import get_settings
from app.models.event import Event
from app.models.extraction_run import ExtractionRun
from app.schemas.extraction import SiteConfiguration
from app.services.attendability import (
    AUDIENCE,
    CANCELLED,
    CERTIFICATION,
    CONFERENCE,
    DEADLINE,
    INTERNAL,
    PROFESSIONAL,
    VIRTUAL,
    hide_unattendable_events,
    not_attendable_reason,
)
from app.services.extraction_runs import preview_extraction, run_extraction
from app.services.website_configuration import approve_configuration
from tests.extraction_helpers import html_handler, patched_http_fetch


@pytest.mark.parametrize(
    "title,reason",
    [
        ("Virtual Book Club", VIRTUAL),
        ("Intro to Grant Writing (Webinar)", VIRTUAL),
        ("Online Info Session: MBA Program", VIRTUAL),
        ("Livestream: Chancellor's Address", VIRTUAL),
        ("PMP Certification Training in Bloomington, IN", CERTIFICATION),
        ("Lean Six Sigma Green Belt 4-Day Classroom", CERTIFICATION),
        ("ServSafe Certificate Course", CERTIFICATION),
        ("2026 Midwest Higher Education Conference", CONFERENCE),
        ("Symposium on Rural Health", CONFERENCE),
        ("Women in Tech Leadership Summit", CONFERENCE),
        ("Last Day to Drop a Class", DEADLINE),
        ("Registration Closes for Spring Intramurals", DEADLINE),
        ("Scholarship Application Deadline", DEADLINE),
        ("Fall Break", DEADLINE),
        ("Thanksgiving Recess Begins", DEADLINE),
        ("Final Exams", DEADLINE),
        ("Classes Begin", DEADLINE),
        ("Grades Due", DEADLINE),
        ("Reminder: Parking Permits Expire", DEADLINE),
        ("Biology Department Faculty Meeting", INTERNAL),
        ("Dissertation Defense: Jane Doe", INTERNAL),
        ("Staff Training: New Expense System", INTERNAL),
        ("Office Hours with the Dean", INTERNAL),
        # Titles seen on the Bloomington and Bethlehem sources.
        ("CANCELED: Full Frontal Comedy Show", CANCELLED),
        ("Pennsylvania Latino Convention 2026", CONFERENCE),
        ("Attendee Registration Indiana Society of Physician Recruiters (INSPR) 2026", CONFERENCE),
        ("Stark Seminar Series: Junfang Wu, PhD", CONFERENCE),
        ("Zachary Celentano - BMB Seminar", CONFERENCE),
        ("Finance and Business Networking | Elevating Your Potential - Indianapolis", PROFESSIONAL),
        ("December Real Estate Masterclass | Roots Realty Co.", PROFESSIONAL),
        ("How To Deal with Difficult Employees for Business Owners, Entrepreneurs, HR",
         PROFESSIONAL),
        ("Integrating Somatic Awareness and Expressive Arts in Clinical Practice", PROFESSIONAL),
        ("Bloomington, Indiana: Empower Your Finances", PROFESSIONAL),
        ("Road to Financial Wellness: Investing for Beginners", PROFESSIONAL),
        ("Medicare 101 Educational Event", PROFESSIONAL),
        ("How to Start a Small Mobile Food Business", PROFESSIONAL),
        ("Grow Your Residual Income!", PROFESSIONAL),
        ("Toddler Storytime", AUDIENCE),
        ("NC Class of 2016 Reunion", AUDIENCE),
        ("Lehigh County Senior Expo", AUDIENCE),
        ("Indianapolis Speed Dating for Singles Age 38-54", AUDIENCE),
        ("IUAA Metro NYC Game Watch: IU Football vs North Texas", AUDIENCE),
        ("SWK-S 502 0001 7611 4268", INTERNAL),
        ("ANAT-D502, Rm IB 317, 12PM-4PM", INTERNAL),
        ("BUS X100 Mentoring Sessions - Lee", INTERNAL),
        ("IUH-Rehab Svc Mtg/Chris Gales/ce", INTERNAL),
        ("6 Lane Reservation", INTERNAL),
        ("812 Consulting Final Round Interviews - Day 1", INTERNAL),
        ("MS1 Exam proctoring", INTERNAL),
        ("Private Meetings", INTERNAL),
        ("Meeting", INTERNAL),
        ("Spring 2027: Winter Session Break (for Winter classes).", DEADLINE),
        ("Spring 2027: 50% Refund deadline for 13-Week classes", DEADLINE),
        ("50% Refund Period Ends for first eight weeks session", DEADLINE),
        ("Spring 2027: eDrop/eAdd Schedule Adjustment and Initial Registration", DEADLINE),
        ("School Closed", DEADLINE),
    ],
)
def test_not_attendable_titles(title, reason):
    assert not_attendable_reason(title=title) == reason


@pytest.mark.parametrize(
    "title",
    [
        "Jazz Night at the Park",
        "Virtual Reality Arcade Night",
        "Spring Break Bash at The Bluebird",
        "Finals Week Pancake Breakfast",
        "Fitness Boot Camp in the Park",
        "Sunset Hike to the Summit",
        "Silent Auction: Bids Close at 8pm",
        "Farmers Market",
        "Comic Con Bloomington",
        "City Council Meeting",
        "Registration Required: Pottery for Beginners",
        "IU Real Estate Club Callout Meeting",
        "Bullwinkles Presents REUNION 2026",
        "YDSA 101!",
        "7th Annual Grassroots Music Seminar and Concert Series Tour",
        "A Master Class with Rahsaan Barber",
        "Senior Night: IU Volleyball vs Purdue",
        "Kappa Sigma Rush Week - Volleyball",
        "Board Game Engage Friday Meetup",
        "Women in Business - Callout Meeting",
        "Hela's Hollow Paranormal Expo",
        # A class or workshop that is a fun thing to do in town stays.
        "Basics of Watercolor Workshop",
        "Pottery Class for Beginners",
        "Empanada Making Class",
        "Halloween Charcuterie Workshop at Soaked Winery",
        "Crimson Investment Group Call Out Meeting",
        "Small Business Saturday Pop Up Shop & Fashion Event",
    ],
)
def test_attendable_titles_are_kept(title):
    assert not_attendable_reason(title=title) is None


def test_platform_as_venue_is_virtual():
    assert not_attendable_reason(title="Book Talk", venue="Online") == VIRTUAL
    assert not_attendable_reason(title="Book Talk", venue="Zoom") == VIRTUAL
    # A real place merely named after something else is kept.
    assert not_attendable_reason(title="Book Talk", venue="Monroe County Library") is None


def test_description_only_counts_when_explicit():
    explicit = "This is a virtual event. A Zoom link will be sent after registration."
    assert not_attendable_reason(title="Book Talk", description=explicit) == VIRTUAL
    in_passing = "Buy tickets online. Parking in the conference center garage."
    assert not_attendable_reason(title="Book Talk", description=in_passing) is None
    closed = "This session is for IU faculty and staff only."
    assert not_attendable_reason(title="Benefits Q&A", description=closed) == INTERNAL
    assert not_attendable_reason(title="Gala", description="By invitation only.") == INTERNAL


def test_schema_org_attendance_mode():
    online = {"eventAttendanceMode": "https://schema.org/OnlineEventAttendanceMode"}
    hybrid = {"eventAttendanceMode": "https://schema.org/MixedEventAttendanceMode"}
    assert not_attendable_reason(title="Talk", raw=online) == VIRTUAL
    assert not_attendable_reason(title="Talk", raw=hybrid) is None


def test_source_category_label():
    assert not_attendable_reason(title="Spring", source_category="Academic Calendar") == DEADLINE
    assert not_attendable_reason(title="Talk", source_category="Online") == VIRTUAL
    assert not_attendable_reason(title="Talk", source_category="Music") is None


# --- the import -------------------------------------------------------------

JSONLD_CONFIG = SiteConfiguration(pattern_name="json_ld_event", listing_url="https://example.com/events")


def _jsonld_page(*events: dict) -> str:
    blocks = "".join(
        f'<script type="application/ld+json">{json.dumps(e)}</script>' for e in events
    )
    return f"<html><head><title>Events</title>{blocks}</head><body></body></html>"


def _event(name: str, slug: str, **extra) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "Event",
        "name": name,
        "startDate": "2030-06-01T19:00:00-05:00",
        "url": f"https://example.com/events/{slug}",
        "location": {"@type": "Place", "name": "Central Park Bandshell"},
        **extra,
    }


def _page_handler(body: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    return handler


@pytest.fixture
async def approved_website(db_session, make_city, make_website, make_user):
    website = make_website(make_city(), name="Src", base_url="https://example.com")
    admin = make_user(email="approver@example.com")
    website.configuration = JSONLD_CONFIG.model_dump(mode="json")
    db_session.commit()
    with patched_http_fetch(html_handler("jsonld_single_event.html")):
        await preview_extraction(db_session, website)
    approve_configuration(db_session, website, approved_by_user_id=admin.id)
    return website


@pytest.mark.asyncio
async def test_import_leaves_out_unattendable_events(db_session, approved_website):
    page = _jsonld_page(
        _event("Jazz Night", "jazz"),
        _event("PMP Certification Bootcamp", "pmp"),
        _event("Book Talk", "talk",
               eventAttendanceMode="https://schema.org/OnlineEventAttendanceMode"),
    )
    with patched_http_fetch(_page_handler(page)):
        result = await run_extraction(db_session, approved_website, triggered_by_user_id=None)

    assert [e.title for e in db_session.query(Event)] == ["Jazz Night"]
    assert result.status == "success"  # a filtered listing is not a broken one
    run = db_session.get(ExtractionRun, result.run_id)
    assert "not_attendable_excluded:2" in run.warnings


@pytest.mark.asyncio
async def test_import_takes_down_an_event_imported_before_the_filter(
    db_session, approved_website, monkeypatch
):
    page = _jsonld_page(_event("Jazz Night", "jazz"), _event("Add/Drop Deadline", "drop"))
    monkeypatch.setattr(get_settings(), "attendability_filter_enabled", False)
    with patched_http_fetch(_page_handler(page)):
        await run_extraction(db_session, approved_website, triggered_by_user_id=None)
    assert db_session.query(Event).filter_by(is_active=True).count() == 2

    monkeypatch.setattr(get_settings(), "attendability_filter_enabled", True)
    with patched_http_fetch(_page_handler(page)):
        await run_extraction(db_session, approved_website, triggered_by_user_id=None)
    db_session.expire_all()
    active = [e.title for e in db_session.query(Event).filter_by(is_active=True)]
    assert active == ["Jazz Night"]


def test_cleanup_dry_run_changes_nothing_then_apply_deactivates(
    db_session, make_city, make_event
):
    city = make_city()
    make_event(city, title="Jazz Night", canonical_url="https://x/1")
    make_event(city, title="Virtual Info Session", canonical_url="https://x/2")

    found = hide_unattendable_events(db_session, apply=False)
    assert [(e.title, r) for e, r in found] == [("Virtual Info Session", VIRTUAL)]
    assert db_session.query(Event).filter_by(is_active=True).count() == 2

    hide_unattendable_events(db_session, apply=True)
    db_session.expire_all()
    assert [e.title for e in db_session.query(Event).filter_by(is_active=True)] == ["Jazz Night"]
