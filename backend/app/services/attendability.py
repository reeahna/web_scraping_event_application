"""Is this something a person can actually go to?

Sources, Eventbrite and university calendars in particular, list a lot that is
not an event in the sense this site means: online-only sessions, professional
conferences, certification courses, internal staff and faculty business, and
calendar entries that are really deadlines or reminders ("Last day to drop",
"Registration closes"). The site is for college students, so it also leaves
out what is aimed at someone else (business networking, real-estate
masterclasses, toddler story times, class reunions) and listings the source
itself marks as cancelled. This decides, from an event's own text, whether it
is one of those, so the import can leave it out.

Deliberately keyword-based and conservative. A rule only fires on wording that
names the thing outright, and the free-text description is only consulted for
explicit statements ("this is a virtual event", "faculty and staff only"),
never for a word in passing, because a description mentioning "online
tickets" or "a conference room" says nothing about the event itself.
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy.orm import Session

VIRTUAL = "virtual"
CONFERENCE = "conference"
CERTIFICATION = "certification"
DEADLINE = "deadline"
INTERNAL = "internal"
PROFESSIONAL = "professional"
AUDIENCE = "audience"
CANCELLED = "cancelled"


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


# --- cancelled ----------------------------------------------------------------
# University calendars keep a cancelled listing up with the word in front.
_CANCELLED_TITLE = _rx(r"^\W*(cancell?ed|postponed)\b")


# --- virtual / online only ---------------------------------------------------
# "Virtual reality" and "virtual tour of an exhibit you are standing in" are the
# classic false positives, so "virtual reality/VR" is carved out.
_VIRTUAL_TITLE = _rx(
    r"\b(virtual(?!\s+reality)|online|webinar|web\s*cast|livestream(ed)?|live[- ]stream(ed)?|"
    r"zoom|teleconference)\b"
)
# The venue/address field holding a platform rather than a place.
_VIRTUAL_PLACE = _rx(
    r"^\s*(online( event| only)?|virtual( event| only)?|zoom( meeting)?|microsoft teams|"
    r"teams|webex|google meet|livestream|web(inar)?|internet|tbd online|to be announced online)"
    r"\s*\.?$"
)
_VIRTUAL_DESCRIPTION = _rx(
    r"\b(this (is|will be) an? (virtual|online)( only)? (event|session|program|meeting)|"
    r"(held|hosted|offered|taking place) (entirely |exclusively )?(virtually|online)|"
    r"online[- ]only|virtual[- ]only|join (us )?(virtually|online) (via|on|through) zoom|"
    r"zoom link will be (sent|emailed|provided))\b"
)

# --- conferences --------------------------------------------------------------
# "Summit" is also a place name ("Sunset at the Summit"), hence the lookbehinds.
_CONFERENCE_TITLE = _rx(
    r"\b(conferences?|conferencia|symposium|colloquium|convention|(?<!the )(?<!at )summit|"
    r"trade show|seminar series|grand rounds|journal club|"
    r"(attendee|exhibitor|vendor|sponsor) registration)\b|"
    # A research talk: "Materials Seminar", "CCBB Seminar Speaker", "Seminar: ..."
    r"\bseminar\b(\s+speaker|\s*[:\-–|])|\w\s+seminar\s*$|^\s*seminar\b"
)

# --- certifications and professional courses ---------------------------------
_CERTIFICATION_TITLE = _rx(
    r"\b(certification|certificate (program|course|training)|accreditation|"
    r"credential(ing)?|exam prep|test prep|ceus?|continuing education|cpe credits?|licensure|"
    r"pmp|capm|itil|six sigma|scrum master|safe agilist|prince2|cissp|comptia|cert prep)\b"
)

# --- aimed at working professionals, or at money rather than fun --------------
_PROFESSIONAL_TITLE = _rx(
    r"\b((business|professionals?|executive|industry|chamber|b2b|realtors?|real estate|"
    r"healthcare|finance|engineering|hospitality|fashion|blockchain|tech|women in business)"
    r"(\s+and\s+business)?\s+networking|"
    r"networking (breakfast|luncheon|lunch|happy hour|mixer) for|"
    r"masterminds?|real estate|realtors?|mortgage|landlords?|property management|"
    r"investors? (meetup|summit|forum)|"
    r"(for|with) (small )?business owners|entrepreneurs, hr|for (hr|human resources) professionals|"
    r"(fair housing|osha|food handler|forklift|cpr/aed for (employers|businesses)) (basics |"
    r"compliance )?training|"
    r"(business|marketing|content|sales|leadership|real estate|investing) master\s?class|"
    r"master\s?class (in|for) (business|marketing|sales|leadership|real estate)|"
    r"(physician|nurse|nursing|clinical|clinician|attorney|legal|accounting|cpa|teacher|"
    r"educator|parish|church|ministry) (leaders|recruiters|managers|professionals)|"
    r"(recruiters|leaders|educators) (association|society|forum)|"
    r"in clinical practice|for clinicians|for therapists|for counselors|for educators|"
    r"for (\w+ )?faculty|employment consultant|"
    # Money and running a business: a pottery or cooking class is a night out,
    # a finance class is not.
    r"(your|personal) finances?|financial (wellness|planning|literacy|freedom|independence)|"
    r"investing for|(residual|passive) income|medicare|social security benefits|"
    r"(retirement|estate|exit|tax|wealth) planning|social selling|"
    r"(grow|strengthen|start|scale|build) (a |your )?(small )?(\w+ ){0,2}business|"
    r"business owners?|small business working session|money is expensive)\b"
)

# --- aimed at someone other than college students ------------------------------
_AUDIENCE_TITLE = _rx(
    r"\b(toddlers?|preschool(ers)?|babies|baby (and|&) me|story ?time|storytime|"
    r"for kids|kids'? (club|camp|class)|homeschool(ers|ing)?|"
    r"high school|middle school|elementary school|class of '?\d{2,4}|"
    r"(\d+(st|nd|rd|th)|class|family|club|alumni|high school) reunion|"
    # One residence-hall floor's own program: "Movie Night for Cravens Floor 1",
    # "Smith 2 & Ed 3: Lucky Charms" (two floors' joint social).
    r"floors? \d+|"
    r"senior u|seniors?['’] (academy|university)|intro to (email|computers|the internet)|"
    r"computer basics|"
    r"senior (expo|citizens?|center)|seniors (55|60|62|65)|older adults|retirees|"
    r"(55|60|62|65)\s*(\+|and (up|over|older))|"
    r"ages? (2[5-9]|[3-9]\d)\s*(-|–|to|\+|and)|"
    r"(alumni|iuaa)\b.*\b(game watch|chapter|reception|happy hour|weekend|reunion)|"
    r"\balumni (association|chapter|club|reception|game watch))\b"
)

# A joint social for two residence-hall floors: "Smith 2 & Ed 3: Lucky Charms".
_FLOOR_PAIR_TITLE = _rx(r"^\s*[a-z]+ \d\s*(&|and)\s*[a-z]+ \d\s*:")

# A campus service's open hours or a study space listed as if it were an event:
# "Drop-In Career Coaching", "Learning Lab", "Study".
_SERVICE_TITLE = _rx(
    r"\bdrop[- ]in (career|advising|tutoring|coaching|hours|help|consultations?|writing)\b|"
    r"\b(career coaching|study tables?|tutoring hours|writing (center|tutor)|advising hours)\b|"
    r"^\s*(study|study hall|learning lab|open lab|lab hours|tutoring|advising|"
    r"quiet study|group study)\s*$|"
    # How to apply to a program: "Learn How to Apply for the 2027 Rural
    # Placemaking Studio", "MBA Information Session".
    r"\b(learn )?how to apply\b|\binfo(rmation(al)?)? sessions?\b|"
    r"\b(volunteer|new member|mentor|tutor|employee) (orientation|training)\b|"
    r"\borientation session\b"
)

# A theme week or month is a banner over other events, not one itself:
# "Wellness Week", "Pride Month".
_THEME_PERIOD_TITLE = _rx(r"^\s*([\w'&-]+\s+){1,3}(week|month)\s*$")

# The description says outright that it is a symposium or conference.
_CONFERENCE_DESCRIPTION = _rx(
    r"\bthis (symposium|conference|colloquium|research seminar)\b|"
    r"\b(alumni|trainees|colleagues|researchers),? (and )?(trainees|colleagues|researchers)\b"
)

# Words that mark a night out or a student group, which beat a conference or
# professional word in the same title ("Grassroots Music Seminar and Concert
# Series", "IU Real Estate Club Callout Meeting").
_STUDENT_DRAW = _rx(
    r"\b(concerts?|festival|fest|party|parties|show|performance|comedy|screening|"
    r"tailgate|trivia|karaoke|open mic|dance|gala|ball|"
    r"club|call[- ]?out|students?|undergrad(uate)?s?)\b"
)

# --- deadlines, academic-calendar dates and reminders --------------------------
_DEADLINE_TITLE = _rx(
    r"\b(deadline|reminder|"
    r"(registration|enrollment|enrolment|applications?|submissions?|nominations?|"
    r"sign[- ]?ups?|proposals?|abstracts?|rsvps?)\s+(is\s+|are\s+)?(now\s+)?"
    r"(opens?|closes?|closing|due|ends?|begins?|period|window)|"
    r"(last|final|first) day (to|for|of classes|of the term|of the semester)|"
    r"add\s*/\s*drop|drop\s*/\s*add|late drop|withdrawal period|priority registration|"
    r"open enrollment|grades? (are\s+)?due|(fees?|tuition|payments?|bills?) (are\s+|is\s+)?due|"
    r"due (date|by)|"
    r"(classes|class|semester|term|session|instruction) (begins?|starts?|ends?|resumes?)|"
    r"no classes|classes cancell?ed|"
    r"(university|campus|school|schools|offices?|libraries|library) (is\s+|are\s+)?closed|"
    r"last day of (open |late )?registration|refund (period|deadline)|"
    r"grade of w|e-?drop|e-?add|schedule adjustment)\b|"
    # Registrar entries come labelled with the term: "Spring 2027: ..."
    r"^\s*(fall|spring|summer|winter)\s+(20\d\d|session)\s*:"
)
# Bare academic-calendar entries: the whole title is the break or exam period,
# so "Spring Break Bash" or "Finals Week Pancake Breakfast" still get through.
_ACADEMIC_PERIOD_TITLE = _rx(
    r"^\s*(fall|spring|winter|summer|thanksgiving|holiday)\s+(break|recess)"
    r"(\s*(begins|ends|starts|[-–:].*))?\s*$|"
    r"^\s*(final exams?|finals week|final examination period|reading days?|exam week)"
    r"(\s*(begins?|ends?|starts?))?\s*$"
)

# --- internal / closed to the public -------------------------------------------
_INTERNAL_ANYWHERE = _rx(
    r"\b((faculty|staff|employees?|members?|students?|residents?|invited guests?|"
    r"(iu|university) (faculty|staff|employees|students|affiliates))(\s+and\s+(faculty|staff|"
    r"students|employees))?\s+only|"
    r"by invitation( only)?|invitation[- ]only|invite[- ]only|private event|"
    r"closed (to the public|event|meeting|session)|not open to the public|"
    r"(iu|university) (username|network id|login|credentials) (is\s+)?required|"
    r"(log|sign) in with your (iu|university) (username|account|credentials))\b"
)
_INTERNAL_TITLE = _rx(
    r"\b((staff|faculty|department(al)?|committee|lab|team|advisory|executive|all[- ]hands)"
    r"\s+meetings?|faculty senate|"
    r"(dissertation|thesis|doctoral|qualifying|prospectus)\s+(defen[cs]e|exam|proposal)|"
    r"office hours|professional development|staff (training|development)|"
    r"employee (training|orientation)|new employee|compliance training|"
    r"(hr|human resources|benefits) (training|session|workshop|webinar|orientation)|"
    # Room bookings and logistics a calendar system publishes alongside events.
    r"(room|lane|court|space|field|table) (reservation|booking|hold)s?|reservations?$|"
    r"maintenance|proctor(ing|ed)?|package (overflow|pickup)|mtg|"
    r"(final round|first round|second round|group|recruitment|candidate|admissions|job|mock)"
    r" interviews?|private meetings?|"
    # Staff recognition and medical-training sessions.
    r"years of service|service awards?|employee (recognition|appreciation)|"
    r"retirement (party|reception|celebration) for|oite|usmle|in-training exam|board review)\b|"
    # A campus office or service listed as if it were an event:
    # "Embedded Accessible Educational Services (AES)".
    r"\bservices\s*(\([A-Z]{2,6}\))?\s*$|"
    r"^\s*(meetings?|closed|hold|reserved|tbd|tba)\s*$"
)
# Case-sensitive: a course section ("SWK-S 502 0001", "NURS-B 444", "ANAT-D502")
# or a room code ("Rm IB 317") at the front of a title is a class or a booking.
_INTERNAL_CODES = re.compile(
    r"^\s*[A-Z]{2,5}(-[A-Z]?\s?\d{3}|\s[A-Z]\d{3})[A-Z]?\b|\bRms?\.? [A-Z]{0,3}\s?\d{2,4}\b|"
    # A room booking signed with the booker's lowercase initials:
    # "IUH-Years of Service Awards/Lori Kern/ce", "IUH-MillionMeals/SGirgis/dmg".
    r"/\s*[a-z]{2,4}\s*$|^\s*IUH\s*-|"
    # A course listed by its number: "FOLK 100: Foundations of our Fields".
    r"^\s*[A-Z]{2,5} [A-Z]?\d{3}[A-Z]?\s*:"
)

# --- the source's own category label -----------------------------------------
_SOURCE_CATEGORY = {
    VIRTUAL: _rx(r"\b(online|virtual|webinars?)\b"),
    CONFERENCE: _rx(r"\b(conferences?|symposi(a|ums?))\b"),
    CERTIFICATION: _rx(r"\b(certifications?|continuing education)\b"),
    DEADLINE: _rx(r"\b(deadlines?|academic calendar|important dates|reminders?)\b"),
    INTERNAL: _rx(r"\b(human resources|faculty and staff|staff only|internal)\b"),
    PROFESSIONAL: _rx(r"\b(business|networking|professional development)\b"),
}

# schema.org: an online-only event declares this attendance mode; a hybrid one
# declares MixedEventAttendanceMode and is kept.
_ONLINE_ATTENDANCE_MODE = "OnlineEventAttendanceMode"


def not_attendable_reason(
    *,
    title: str | None,
    description: str | None = None,
    venue: str | None = None,
    address: str | None = None,
    source_category: str | None = None,
    raw: dict[str, Any] | None = None,
) -> str | None:
    """Why this is not an event someone can turn up to, or None when it is.

    The reason is one of CANCELLED, VIRTUAL, CONFERENCE, CERTIFICATION,
    DEADLINE, INTERNAL, PROFESSIONAL or AUDIENCE, recorded so an excluded event
    can be explained."""
    title = title or ""
    description = description or ""

    if _CANCELLED_TITLE.search(title):
        return CANCELLED

    if raw is not None:
        raw_text = json.dumps(raw, default=str)
        if _ONLINE_ATTENDANCE_MODE in raw_text and "MixedEventAttendanceMode" not in raw_text:
            return VIRTUAL
    if _VIRTUAL_TITLE.search(title) or _VIRTUAL_DESCRIPTION.search(description):
        return VIRTUAL
    if any(_VIRTUAL_PLACE.match(v) for v in (venue, address) if v):
        return VIRTUAL

    if _DEADLINE_TITLE.search(title) or _ACADEMIC_PERIOD_TITLE.search(title):
        return DEADLINE
    if (
        _INTERNAL_TITLE.search(title)
        or _INTERNAL_CODES.search(title)
        or _INTERNAL_ANYWHERE.search(f"{title}\n{description}")
    ):
        return INTERNAL
    if _CERTIFICATION_TITLE.search(title):
        return CERTIFICATION
    if _SERVICE_TITLE.search(title):
        return INTERNAL
    if _THEME_PERIOD_TITLE.search(title):
        return DEADLINE
    if _AUDIENCE_TITLE.search(title) or _FLOOR_PAIR_TITLE.search(title):
        return AUDIENCE
    if not _STUDENT_DRAW.search(title):
        if _CONFERENCE_TITLE.search(title) or _CONFERENCE_DESCRIPTION.search(description):
            return CONFERENCE
        if _PROFESSIONAL_TITLE.search(title):
            return PROFESSIONAL

    if source_category:
        for reason, pattern in _SOURCE_CATEGORY.items():
            if pattern.search(source_category):
                return reason
    return None


def candidate_not_attendable_reason(candidate) -> str | None:
    """not_attendable_reason for an in-flight EventCandidate."""
    return not_attendable_reason(
        title=candidate.title,
        description=candidate.description,
        venue=candidate.venue,
        address=candidate.address,
        source_category=candidate.source_category,
        raw=candidate.raw,
    )


def event_not_attendable_reason(event) -> str | None:
    """not_attendable_reason for a stored Event (whose raw record is not kept)."""
    return not_attendable_reason(
        title=event.title,
        description=event.description,
        venue=event.public_venue,
        address=event.public_address,
        source_category=event.source_category,
    )


def hide_unattendable_events(db: Session, *, apply: bool) -> list[tuple[Any, str]]:
    """Find active, unarchived events that are not attendable; deactivate them
    when `apply` is set. Returns (event, reason) for each one found, so a dry
    run can be reviewed first. Used to clean up events imported before the
    import-time filter existed."""
    from app.models.event import Event

    found: list[tuple[Any, str]] = []
    query = db.query(Event).filter(Event.is_active.is_(True), Event.archived_at.is_(None))
    for event in query.order_by(Event.id):
        reason = event_not_attendable_reason(event)
        if reason:
            found.append((event, reason))
            if apply:
                event.is_active = False
    if apply:
        db.commit()
    return found
