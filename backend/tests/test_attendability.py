"""Leaving out what is not an event a person can go to."""

import json

import httpx
import pytest

from app.config import get_settings
from app.models.event import Event
from app.models.extraction_run import ExtractionRun
from app.repositories.event import decode_stored_html_entities
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
from app.services.fingerprints import title_key
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
        ("Smith 2 & Ed 3: Lucky Charms", AUDIENCE),
        ("Movie Night for Cravens Floor 1", AUDIENCE),
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
        ("IUH-Years of Service Awards/Lori Kern/ce", INTERNAL),
        ("Years of Service Awards", INTERNAL),
        ("SPINE: OITE Review", INTERNAL),
        ("Embedded Accessible Educational Services (AES)", INTERNAL),
        ("Drop-In Career Coaching", INTERNAL),
        ("Learning Lab", INTERNAL),
        ("Study", INTERNAL),
        ("FOLK 100: Foundations of our Fields", INTERNAL),
        ("Wellness Week", DEADLINE),
        ("Learn How to Apply for the 2027 Rural Placemaking Studio", INTERNAL),
        ("Senior U: Intro to Email and Internet", AUDIENCE),
        ("VITAL Volunteer Orientation Session - Teaching English as a New Language", INTERNAL),
        ("Corvalan Collab_Individual Clients", INTERNAL),
        ("Elston Group Meeting - First Years", INTERNAL),
        ("Internal Medicine Residency Didactics", INTERNAL),
        ("Studying", INTERNAL),
        ("Mysticism Reading Group", INTERNAL),
        ("Dame’s meeting", INTERNAL),
        ("CCBB Seminar Guest Speaker: Dr. Tianyi Mao 10/9/26", INTERNAL),
        # A course's own class meetings.
        ("Monday Lecture", INTERNAL),
        ("Monday Lessons", INTERNAL),
        ("Thursday Class", INTERNAL),
        ("Lecture", INTERNAL),
        ("Lecture 5", INTERNAL),
        ("Class Lecture - Week 3", INTERNAL),
        ("Week 3 Lecture: Enzyme Kinetics", INTERNAL),
        ("Biology 1010 Lecture", INTERNAL),
        ("Recitation", INTERNAL),
        ("Discussion Section 2", INTERNAL),
        ("Class Session: Intro to Statistics", INTERNAL),
        # Seen on the live site in the first daily review.
        ("Executive Board Meeting", INTERNAL),
        ("GPSG Cabinet Meetings", INTERNAL),
        ("WAC Weekly Leadership Meeting", INTERNAL),
        ("Meeting #2", INTERNAL),
        ("GSSO Escape Room Setup", INTERNAL),
        ("Kill Roy: Volume 10 Pre-Event Meeting", INTERNAL),
        ("Instructor Luncheon (Closed)", INTERNAL),
        ("LIVEWHALE TEST EVENT", INTERNAL),
        ("ICMH Journal Club", INTERNAL),
        ("BMBP Presents Faculty Candidate David L. Mobley", INTERNAL),
        ("Introduction to Limited Submissions", INTERNAL),
        ("Faculty and Staff Flu Shot Clinics", INTERNAL),
        ("Experiential Learning Series: Creating a Community-Engaged Course Syllabus", INTERNAL),
        ("Simplifying Complex Concepts in the Classroom", INTERNAL),
        ("eShipGlobal System Training", INTERNAL),
        ("Help Center Training Weekend", INTERNAL),
        ("IU Football Parking - October 17 vs. Ohio State", INTERNAL),
        ("Move your Car by 6 p.m. - Ohio State vs IU", INTERNAL),
        ("Trellis Student Financial Wellness Survey", INTERNAL),
        ("IUSON - H356 Group 1", INTERNAL),
        ("IUSM - FCP1 small groups", INTERNAL),
        ("Tanner H399 Honors", INTERNAL),
        ("Motor Speech Study Session", INTERNAL),
        ("Izzy study seshhhh", INTERNAL),
        ("Anatomy Review, Exam 2", INTERNAL),
        ("Discussion 10/20", INTERNAL),
        ("Stellic Drop-ins", INTERNAL),
        ("Chill/Study Night", INTERNAL),
        ("AI Innovation Hub Open Office (in-person, IU Bloomington)", INTERNAL),
        ("Team Captain Meeting", INTERNAL),
        ("New Faculty Teaching Conversations: Share an Idea, Leave with Many", INTERNAL),
        ("Teaching for a Multilingual Campus: Practical Strategies for Access", INTERNAL),
        ("Improving Reading Comprehension: Why Students Struggle & What You Can Do",
         INTERNAL),
        ("Scholarly Writing Program Fall Retreat", INTERNAL),
        ("Senior Planet: AI & Disinformation", AUDIENCE),
        ("College Alumni Awards Celebration", AUDIENCE),
        ("Cheri Goodall - Chemical Biology Supergroup", INTERNAL),
        ("Prospective Doctoral Student Open House", AUDIENCE),
        ("Academy of Teaching Scholars: Best Practices in Global Health Education", INTERNAL),
        ("Care Support Community of Practice: Health Literacy", INTERNAL),
        ("Study group (A,K,H)", INTERNAL),
        ("MC", INTERNAL),
        ("VL", INTERNAL),
        ("Trunk or Treat - Pediatrics SIG", AUDIENCE),
        ("Using VoiceThread to Engage Students*", INTERNAL),
        ("WeVideo: Student Centered Learning with Learner Made Interactive Video", INTERNAL),
        ("City of Bethlehem’s Annual Trick or Treat Night", AUDIENCE),
        ("BTAA WIT Coffee Catch-up: Cyber Safety/Security Panel", PROFESSIONAL),
        ("2026 Game Watch Football IUAA Nashville IU vs Ohio State", AUDIENCE),
        ("IUAA Colorado: Hoosier Halloween", AUDIENCE),
        ("Teen Winter Skate Night", AUDIENCE),
        ("Tot-Tastic Day Off", AUDIENCE),
        ("Art Hive for 50+", AUDIENCE),
        ("Emeriti House Current Events", AUDIENCE),
        ("AI Strategy for Executives: Organizational Adoption and Execution", PROFESSIONAL),
        ("Anxiety at Work (for People Leaders)", PROFESSIONAL),
        ("Fundamentals of Project Management", PROFESSIONAL),
        ("Access MBA- Bogota, Columbia", PROFESSIONAL),
        ("Bloomington, IN Roadshow- A free, Five Day Only Buying Event", PROFESSIONAL),
        ("Autism Echo & Developmental Disabilites.", PROFESSIONAL),
        ("Road to Retirement: Fundamentals of Retirement Income Planning", PROFESSIONAL),
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
        "Foraged, Found and Reimagined: Selected Works by Trish Korte",
        "Home Away from Home: Student Life on Campus",
        "Rock/Pop Night",
        "Club 3 and Me",
        "Waltz 101 (Q-Project)",
        "Book Club Social",
        "Echo & the Bunnymen Tribute Night",
        "Corporeality: Living in Our Bodies | Kinsey Institute",
        "Study Abroad Fair",
        "Wellness Week Yoga in the Park",
        "Shark Week Watch Party",
        "Top 40 Night at the Bluebird",
        # A class or workshop that is a fun thing to do in town stays.
        "Basics of Watercolor Workshop",
        "Pottery Class for Beginners",
        "Empanada Making Class",
        "Halloween Charcuterie Workshop at Soaked Winery",
        "Crimson Investment Group Call Out Meeting",
        "Small Business Saturday Pop Up Shop & Fashion Event",
        # Public and named lectures are talks people go to.
        "2026 Thomas H. Lake Lecture with Meghan Sullivan",
        "One Person, No Vote: A lecture and Q&A with Carol Anderson",
        "Lecture: Rivers",
        "Guest Lecture: AI Ethics",
        "Monday Night Lecture Series: The History of Jazz",
        "General Board Meeting",
        "Free Lindy Hop Dance Lessons (Beginner)",
        "Weekly Latin Dance Lessons",
        "WellSpace Drop-In Series: Meet SWAC",
        "Bi-Weekly Application Accountability Night",
        "Superhero Movie Night",
        "Bible Study Group",
        "Halloween Treats & Tricks Magic Show",
        "DJ Night",
        "Supergroup: A Tribute to The Traveling Wilburys",
        "Halloween Trivia Night",
        "Three Minute Thesis Prep Event (October)",
        "Senior Recital – Jane Doe, violin",
        "Teaching Kids to Code: A Volunteer Fair",
        "General Body Meeting",
        "Board of Programmers Meeting",
        "Study Abroad Fair",
        "The World is Your Classroom!",
        "Building a Successful MCAT Study Plan",
        "IU Football Watch Party: Hoosiers vs. Ohio State",
        "Janette Fishell, organ; remarks by Jesse Eschbach (University of North Texas, Emeritus)",
    ],
)
def test_attendable_titles_are_kept(title):
    assert not_attendable_reason(title=title) is None


def test_description_naming_a_symposium():
    reason = not_attendable_reason(
        title="Beyond the Bench: Honoring Dr. Gary Landreth’s Contributions to Science",
        description="This symposium will bring together alumni, trainees, colleagues and "
        "researchers who will share stories.",
    )
    assert reason == CONFERENCE


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


def test_cleanup_decodes_html_entities_left_in_stored_titles(db_session, make_city, make_event):
    city = make_city()
    make_event(city, title="Rock &amp;amp; Roll", venue="Bear&#8217;s Place",
               canonical_url="https://x/1")
    make_event(city, title="Jazz & Blues", canonical_url="https://x/2")

    assert [e.title for e in decode_stored_html_entities(db_session, apply=False)] == [
        "Rock &amp;amp; Roll"
    ]
    decode_stored_html_entities(db_session, apply=True)
    db_session.expire_all()
    event = db_session.query(Event).filter_by(canonical_url="https://x/1").one()
    assert (event.title, event.venue, event.normalized_title) == (
        "Rock & Roll", "Bear’s Place", title_key("Rock & Roll")
    )


@pytest.mark.asyncio
async def test_scheduler_sweep_takes_down_unattendable_events(db_session, make_city, make_event):
    from app.scheduler.runtime import SchedulerRuntime

    city = make_city(name="Bloomington", slug="bloomington", state_or_region="Indiana")
    make_event(city, title="Jazz Night", canonical_url="https://x/1")
    make_event(city, title="Monday Lecture", canonical_url="https://x/2")

    runtime = SchedulerRuntime(holder="test")
    await runtime._attendability_sweep_tick()  # not the leader: leaves everything
    db_session.expire_all()
    assert db_session.query(Event).filter_by(is_active=True).count() == 2

    runtime._is_leader = True
    await runtime._attendability_sweep_tick()
    db_session.expire_all()
    assert [e.title for e in db_session.query(Event).filter_by(is_active=True)] == ["Jazz Night"]



@pytest.mark.parametrize(
    "url,internal",
    [
        ("https://events.iu.edu/campus-center/event/2454799-cell-biology", True),
        ("https://events.iu.edu/iuhumanrescal/event/2333565-lean-and-ai-20", True),
        ("https://events.iu.edu/hsb/event/2396885-simucase-group-4", True),
        ("https://events.iu.edu/live/event/123-jazz-night", False),
        ("https://example.com/campus-center/event/1", False),
        (None, False),
    ],
)
def test_room_booking_calendar(url, internal):
    reason = not_attendable_reason(title="Jazz Night", url=url)
    assert (reason == INTERNAL) is internal


@pytest.mark.parametrize(
    "url,alumni",
    [
        ("https://events.iu.edu/iu-alumni/event/2355473-raclin-legends-honors-night", True),
        ("https://events.iu.edu/live/event/123-jazz-night", False),
    ],
)
def test_alumni_calendar(url, alumni):
    reason = not_attendable_reason(title="Jazz Night", url=url)
    assert (reason == AUDIENCE) is alumni


def test_named_lecture_on_alumni_calendar_is_kept():
    url = "https://events.iu.edu/iu-alumni/event/2408095-michael-r-cohen-lectureship"
    assert not_attendable_reason(title="Michael R. Cohen Lectureship", url=url) is None
