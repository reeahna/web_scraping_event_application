"""Is this something a person can actually go to?

Sources, Eventbrite and university calendars in particular, list a lot that is
not an event in the sense this site means: online-only sessions, professional
conferences, certification courses, internal staff and faculty business, and
calendar entries that are really deadlines or reminders ("Last day to drop",
"Registration closes"). This decides, from an event's own text, whether it is
one of those, so the import can leave it out.

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


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


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
_CONFERENCE_TITLE = _rx(r"\b(conference|symposium|colloquium|(?<!the )(?<!at )summit)\b")

# --- certifications and professional courses ---------------------------------
_CERTIFICATION_TITLE = _rx(
    r"\b(certification|certificate (program|course|training)|accreditation|"
    r"credential(ing)?|exam prep|test prep|ceus?|continuing education|cpe credits?|licensure|"
    r"pmp|capm|itil|six sigma|scrum master|safe agilist|prince2|cissp|comptia)\b"
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
    r"(university|campus|offices?|libraries|library) (is\s+|are\s+)?closed)\b"
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
    r"(hr|human resources|benefits) (training|session|workshop|webinar|orientation))\b"
)

# --- the source's own category label -----------------------------------------
_SOURCE_CATEGORY = {
    VIRTUAL: _rx(r"\b(online|virtual|webinars?)\b"),
    CONFERENCE: _rx(r"\b(conferences?|symposi(a|ums?))\b"),
    CERTIFICATION: _rx(r"\b(certifications?|continuing education)\b"),
    DEADLINE: _rx(r"\b(deadlines?|academic calendar|important dates|reminders?)\b"),
    INTERNAL: _rx(r"\b(human resources|faculty and staff|staff only|internal)\b"),
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

    The reason is one of VIRTUAL, CONFERENCE, CERTIFICATION, DEADLINE or
    INTERNAL, recorded so an excluded event can be explained."""
    title = title or ""
    description = description or ""

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
    if _INTERNAL_TITLE.search(title) or _INTERNAL_ANYWHERE.search(f"{title}\n{description}"):
        return INTERNAL
    if _CERTIFICATION_TITLE.search(title):
        return CERTIFICATION
    if _CONFERENCE_TITLE.search(title):
        return CONFERENCE

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
