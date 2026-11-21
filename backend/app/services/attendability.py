"""Is this something a person can actually go to?

Sources, Eventbrite and university calendars in particular, list a lot that is
not an event in the sense this site means: online-only sessions, professional
conferences, certification courses, internal staff and faculty business, and
calendar entries that are really deadlines or reminders ("Last day to drop",
"Registration closes"). The site is for college students, so it also leaves
out what is aimed at someone else (executive networking, real-estate
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
from urllib.parse import urlsplit

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
_CANCELLED_TITLE = _rx(
    r"^\W*(cancell?ed|postponed)\b|"
    # Marked after the name: "Rethinking Peer Review *CANCELED*", "Talk (Cancelled)".
    r"[*(\[]\s*(cancell?ed|postponed)\s*[*)\]]|[-–:]\s*(cancell?ed|postponed)\s*$"
)


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
    # Networking open to anyone in town (a tech or business mixer) is fine for
    # students and young adults; networking for executives, chambers of
    # commerce and realtors is not.
    r"\b((executive|chamber|b2b|realtors?|real estate)\s+networking|"
    r"masterminds?|btaa|ncta short courses?|"
    r"real estate|realtors?|mortgage|landlords?|property management|"
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
    r"(retirement|estate|exit|tax|wealth) (income )?planning|road to retirement|social selling|"
    # Project ECHO: case-based tele-mentoring for clinicians ("Autism Echo").
    r"(autism|disabilit(y|ies)|health|clinical|pain|opioid|dementia|psychiatry) echo|"
    r"project echo|"
    r"(grow|strengthen|start|scale|build) (a |your )?(small )?(\w+ ){0,2}business|"
    r"business owners?|small business working session|money is expensive|"
    r"for (executives|people leaders|managers|supervisors)|project management|buying event|"
    r"access mba)\b"
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
    r"computer basics|senior planet|mba campus visits?|"
    r"family (art labs?|art projects?|workshops?|series|playtime|craft-?ernoon)|"
    # Preschool cartoon characters: "Breakfast with Bluey and Bingo".
    r"bluey|paw patrol|peppa pig|daniel tiger|cocomelon|"
    # Library programs for little ones and older adults: "Baby Time", "Kids
    # Chess Club", "Little Play Date!", "Chair-Based Exercise Class".
    r"baby (time|playgroups?)|playgroups?|play ?dates?|kids \w+ club|youth thea(ter|tre)|"
    r"chair[- ]based exercise|"
    # "Children's Drawing from the Masters"; a restaurant's "Kids Eat Free" night.
    r"children['’]s (drawing|art|story|programs?|class(es)?|workshops?)|kids eat free|"
    # Library and parks programs for little ones, kids and older adults:
    # "Babytime", "ToddlerTime", "Terrific Twos", "MMSD Play & Learn",
    # "Kids LEGO Masters", "Tween Manga Club", "Get to Know the Guitar: Kids
    # Edition", "Children's Learn to Swim", "Flag Football - 10U Coed Division",
    # "Zumba Gold", "Senior Ballet Programming", "Chair Yoga".
    r"baby ?play|babytime|toddler ?time|wonderful ones|terrific twos|play (&|and) learn|"
    r"^\s*kids\b|kids (edition|art workshop)|tweens?|children['’]s learn to swim|"
    r"youth martial arts?|4-h|\d{1,2}u (coed )?division|cuentos familiares|"
    r"zumba gold|senior ballet|(therapy|fitness|yoga|exercise|tech help) for seniors|"
    # "Baby Laptime", "Talk With Your Baby", "Saturday Baby Movers", "Mama & Baby
    # Flow", "Children's International Story Hour", "Stay-n-Play", "TK-5th
    # Playtime", "Littlest Sewists", "Wee-One Wednesdays", "Made by Me & My
    # Grown-up", "Unbooked Kids", "DIY Kids | Yarn Weaving", "Ready for
    # Kindergarten", "After School Snack", "Chair Aerobics".
    r"baby (laptime|lapsit|movers)|with your baby|mama (&|and) baby|story hour|"
    r"stay[- ]?(n|and|&)[- ]?play|\btk-\d+(st|nd|rd|th)?|littlest|wee[- ]ones?|my grown-?up|"
    r"\bkids(?=\s*(\||:|$))|kindergarten|younger listeners|after[- ]school|chair aerobics|"
    r"chair yoga|ageless grace|"
    r"(trunk|trick)[- ]or[- ]treats?|prospective (\w+ )?students?|"
    r"senior (expo|citizens?|center)|seniors (55|60|62|65)|older adults|retirees|"
    r"(50|55|60|62|65)\s*(\+|and (up|over|older))|teens?|tots?|emeriti|"
    r"ages? (2[5-9]|[3-9]\d)\s*(-|–|to|\+|and)|"
    # An age range that tops out below 18: "Family Workshop, Ages 6-10".
    r"ages?:? \d{1,2}\s*(-|–|to)\s*(1[0-7]|[1-9])\b|"
    r"(alumni|iuaa)\b.*\b(game watch|chapter|reception|happy hour|weekend|reunion)|"
    r"game watch\b.*\b(alumni|iuaa)|iuaa|"
    # A school's alumni club for another region: "Brown Club of Greater Miami".
    r"\bclub of greater\b|"
    r"\balumni (association|chapter|club|reception|game watch|awards?))\b|"
    # "Art Hive for 50+": the "+" ends the title, so no word boundary follows.
    r"\b(50|55|60|62|65)\s*\+"
)

# "All-Ages Day at the Southwest Teen Space", "Board Games for All Ages": the
# title itself says everyone is welcome, so a word like "teen" (here the room's
# name) doesn't make it a program for one age group. Only the title counts.
_ALL_AGES_TITLE = _rx(r"\ball[- ]ages\b")

# A title that says "all ages" but still states an age limit ("All Ages Teen
# Night, ages 13-18", "All-Ages Show (21+)", "Teens Only") is still limited.
_AGE_LIMIT_TITLE = _rx(
    r"\bages?:? \d{1,2}\s*(-|–|to|\+|and)|\b\d{1,2}\s*(\+|and (up|over|older))|"
    r"\b(teens?|tweens?|kids|children|adults|seniors?|youth|students) only\b|"
    r"\bonly for (teens?|tweens?|kids|children|adults|seniors?|youth|students)\b|"
    r"\bunder \d{1,2}\b"
)


# A joint social for two residence-hall floors: "Smith 2 & Ed 3: Lucky Charms".
_FLOOR_PAIR_TITLE = _rx(r"^\s*[a-z]+ \d\s*(&|and)\s*[a-z]+ \d\s*:")

# A campus service's open hours or a study space listed as if it were an event:
# "Drop-In Career Coaching", "Learning Lab", "Study".
_SERVICE_TITLE = _rx(
    r"\bdrop[- ]in (career|advising|tutoring|coaching|hours?|help|consultations?|writing)\b|"
    # A building's open hours: "Building Hours - RSF".
    r"^\s*building hours\b|"
    r"\b(career coaching|study tables?|tutoring hours|writing (center|tutor)|advising hours)\b|"
    # "Drop-in Academic Coaching", "Study Abroad Walk-In Hours", "Adult Tutoring",
    # "Veteran Services Drop In".
    r"\bacademic coaching\b|\bwalk[- ]in hours\b|^\s*adult tutoring\b|\bservices drop[- ]in\b|"
    # Library and campus help desks: "Technology Help @Central", "Tiger Tech
    # Help", "College & Career Drop In", "Free Legal Clinic", "Time/Task
    # Management Coaching (one-on-one appointments)", "Connect with Your Area
    # Coordinator: Kim!", "Digital Skills for Life - Google Apps", "ESOL Class".
    r"\btech(nology)? help\b|\bcareer drop[- ]in\b|\bfree legal (help|clinic)\b|"
    r"\bone-on-one appointments?\b|^\s*connect with your area coordinator\b|"
    r"^\s*digital skills for life\b|\bneighborhood social worker\b|"
    r"\bpeer recovery supporter\b|\bhome energy assistance\b|"
    r"^\s*(citizenship|esol) class\b|^\s*school help\s*$|"
    # A study group's own booking: "Motor Speech Study Session", "LGSA Study Hour".
    r"\bstudy (sesh\w*|sessions?|hours?|nights?|days?)\b|^\s*study groups?\b|"
    # A course's exam review: "Introduction to IP Review Session: Patents".
    r"\breview sessions?\b|"
    # "Study hall event", "Study w/ the SIAs".
    r"^\s*study hall\b|^\s*study (w/|with)\s|\bexam \d\b|"
    # A campus tool's open hours: "Stellic Drop-ins".
    r"^\s*\w+ drop[- ]ins\s*$|"
    r"^\s*(study|studying|study hall|learning lab|open lab|lab hours|tutoring|advising|"
    r"quiet study|group study)\s*$|"
    # How to apply to a program: "Learn How to Apply for the 2027 Rural
    # Placemaking Studio", "MBA Information Session".
    r"\b(learn )?how to apply\b|\binfo(rmation(al)?)? sessions?\b|"
    r"\b(volunteer|new member|mentor|tutor|employee) (orientation|training)\b|"
    r"\borientation session\b|"
    # Appointments with a clinic or service: "Corvalan Collab_Individual Clients".
    r"(\b|_)individual (clients|sessions|appointments)\b"
)

# A course's own class meetings, which a university calendar can list next to
# its public talks: "Monday Lecture", "Monday Lessons", "Lecture 5", "Week 3 Lecture: Enzymes",
# "BIOL 1010 Lecture", "Recitation". A named or public lecture ("Thomas H.
# Lake Lecture with Meghan Sullivan", "Lecture: Rivers", "A lecture and Q&A
# with ...") says more than this and is kept.
_CLASS_SESSION_TITLE = _rx(
    r"^\s*((mon|tues|wednes|thurs|fri|satur|sun)day|daily|weekly|class|course|morning|"
    r"afternoon|evening)?\s*(lectures?|lessons?|class(es)?)\s*"
    r"(\d+|[-–:]?\s*(week|day|unit|module|part)\s*\d+)?\s*$|"
    r"^\s*(week|unit|module)\s*\d+\b.*\blectures?\b|"
    r"\b\d{3,4}[a-z]?\s+lectures?\b|"
    r"\b(class|course) (lectures?|sessions?|meetings?)\b|"
    r"\b(recitation|discussion|lab|lecture) sections?\b|^\s*recitations?\b|"
    r"^\s*(discussion|lab)\s*(\d+(/\d+)?)?\s*$"
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
    r"^\s*(fall|spring|summer|winter)\s+(20\d\d|session)\s*:|"
    # Closures and holiday notices: "Library Early Closure", "Closed – Main Library",
    # "Walter Center Closed", "Close at 5:00 p.m. for Thanksgiving", "UNIVERSITY
    # HOLIDAY - Thanksgiving Day", "Thanksgiving Holiday - City Facilities Closed".
    r"^\s*closed\b|^[\w .'’-]{0,40}\bclosed\s*$|\b(early )?closures?\b|"
    r"\bclos(e|es|ing) early\b|^\s*close at [\w:. ]+ for\b|"
    r"\b(library|center|building|office) closes\b|"
    r"\bclosed for\b|\bis closed\b|\bfacilities closed\b|"
    r"^\s*(academic and administrative|university|town|city) holiday\b|"
    r"^\s*(thanksgiving( day| holidays?)?|friday after thanksgiving|labor thanksgiving)\s*$|"
    r"\bthanksgiving hours\b|"
    # Calls and sign-ups: "Women's Week 2027 Call for Proposals", "Register for
    # OFA Dance Community Courses", "Involvement Fair [Tuesday Sign-Ups]", "Apply
    # to West House", "Rotaract Club Membership Application", "Thanksgiving To
    # Go Order Pick Up".
    r"\bcall for (proposals|artists|papers|submissions|entries)\b|^\s*register for\b|"
    r"\[\w+ sign-?ups\]|^\s*apply (to|for)\b|\bmembership application\b|"
    r"\bapply\b.*\btoday!?\s*$|\border pick ?-?up\b|^\s*\[class full\]"
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
# A department's own working group, by title: "Sociolinguistics Discussion
# Group", "Neuroscience Journal Club", "Elston Lab Group", "Syntax-Semantics
# Reading Group". A book club stays listed.
_WORKING_GROUP_TITLE = _rx(
    r"\b(reading|discussion) groups?\b|\bjournal clubs?\b|"
    r"\b(lab|research) (group|team)s?\b(?!\s+(open house|tours?|showcase|exhibit))|"
    r"\b(works?[- ]in[- ]progress|wip) (seminar|session|talk|series|workshop)s?\b|"
    r"\b(thesis|dissertation) (writing )?(group|support group|boot ?camp)s?\b"
)

# An academic talk, whoever it is aimed at: checked before the student-draw
# words, so "Physics Colloquium: Teaching Students ..." and "Graduate Student
# Seminar Series" are still left out. A performance, screening, exhibition or
# hands-on workshop is kept ("Lecture-Recital", "Gallery Talk").
_ACADEMIC_TALK_TITLE = _rx(
    r"\b(colloqui(um|a)|seminar series|lecture series|speaker series|guest (speaker|lecture)s?|"
    r"(public|guest|named|memorial|distinguished|keynote) lectures?|keynote( address)?|"
    r"brown bag (talk|seminar|lecture|lunch talk|series)s?|"
    r"exit seminar|research (presentations?|symposium|talks?|seminar)|poster session|"
    r"student presentations|ph\.?\s?d\.? candidate|(thesis|dissertation) presentations?)\b|"
    r"[|:\-–]\s*lecture\s*$|^\s*\w+ brown bag\s*:"
)
_PERFORMANCE_OR_HANDS_ON = _rx(
    r"\b(concerts?|recitals?|performances?|screenings?|exhibitions?|exhibits?|gallery|"
    r"artist|studio|workshop|book club|festival|party|show)\b"
)

_INTERNAL_TITLE = _rx(
    r"\b((staff|faculty|department(al)?|committee|lab|team|advisory|executive|all[- ]hands)"
    r"\s+meetings?|faculty senate|"
    r"(dissertation|thesis|doctoral|qualifying|prospectus)\s+(defen[cs]e|exam|proposal)|"
    r"student defen[cs]e|"
    r"(open )?office hours|open office|professional development|staff (training|development)|"
    r"employee (training|orientation)|new employee|compliance training|"
    r"(hr|human resources|benefits) (training|session|workshop|webinar|orientation)|"
    # Room bookings and logistics a calendar system publishes alongside events.
    r"(room|lane|court|space|field|table) (reservation|booking|hold)s?|reservations?$|"
    r"maintenance|proctor(ing|ed)?|package (overflow|pickup)|mtg|"
    r"(final round|first round|second round|group|recruitment|candidate|admissions|job|mock)"
    r" interviews?|private meetings?|"
    # Staff recognition and medical-training sessions.
    r"years of service|service awards?|employee (recognition|appreciation)|"
    r"retirement (party|reception|celebration) for|oite|usmle|in-training exam|board review|"
    # A lab's own meeting ("Elston Group Meeting - First Years") and teaching
    # for medical residents ("Internal Medicine Residency Didactics").
    r"(residency|fellowship|resident) (didactics|conference|lectures?)|didactics)\b|"
    r"^\s*\w+ group meetings?\b|\b\w+ supergroup\s*$|^\s*\w+['’]s meetings?\s*$|\breading group\b|"
    # A campus office or service listed as if it were an event:
    # "Embedded Accessible Educational Services (AES)".
    r"\bservices\s*(\([A-Z]{2,6}\))?\s*$|"
    r"^\s*(meetings?|closed|hold|reserved|tbd|tba)\s*(#?\d+)?\s*$|"
    # A club's officers meeting among themselves: "Executive Board Meeting",
    # "GPSG Cabinet Meetings", "WAC Weekly Leadership Meeting".
    r"\b(executive|exec|e-?board|(?<!general )board|cabinet|leadership)"
    r"( weekly)?( board)? meetings?\b|"
    r"\b(set[- ]?up|pre-event meeting)\s*$|"
    r"\b(captains?|chairs?|treasurers?|event managers?)['’]? meetings?\b|"
    r"\(closed\)|\btest event\b|"
    r"\bjournal club\b|\bvisiting resident\b|\bfaculty candidate\b|\bjob talk\b|"
    r"\blimited submissions\b|^\s*faculty (and|&) staff\b|"
    # Leadership and process training for staff: "Academic Leadership Series:
    # Current Conversations with OVPFAA", "LehighLEAN Metrics".
    r"^\s*academic leadership series\b|\blehighlean\b|"
    # Teaching development for instructors.
    r"\b(syllabus|rubric|course-specific|student evaluations?)\b|\bin the classroom\b|"
    r"^\s*new faculty\b|^\s*teaching (for|in) an? \w+ (campus|classroom)\b|"
    r"\bwhy students struggle\b|\bscholarly writing (program|retreat)\b|"
    r"\bacademy of teaching scholars\b|\bcommunity of practice\b|\bto engage students\b|"
    r"\bstudent[- ]centered learning\b|\bmeeting the needs of (\w+ )?students\b|\bplayposit\b|"
    r"\bhigh-impact (teaching )?practices\b|\bwevideo\b|\bmaking courses accessible\b|"
    r"^\s*setting up canvas\b|\bfaculty learning communit(y|ies)\b|"
    r"\bdeveloping an? (study abroad|online|new) (program|course)\b|"
    r"\b(deepen|improve|enhance) student learning\b|\binto your (course|curriculum|syllabus)\b|"
    r"\bscholarship of teaching and learning\b|"
    # A teaching tool demo for instructors: "Google NotebookLM for Teaching and Learning".
    r"\bfor teaching and learning\s*$|"
    # Staff training: "Academic Leader Learning Series: Team Design",
    # "LARC R3 Facility Basic Orientation-In-Person Training".
    r"\bacademic leader(ship)? learning series\b|\bfacility (basic )?orientation\b|"
    # A shop's delivery order, not an event: "Wake up to donuts ... Order by 10pm Friday."
    r"\border by (noon|\d{1,2}(:\d\d)?\s*[ap]\.?m)\b|"
    r"\bresearch refresher\b|"
    r"\b(system|educator|staff|volunteer|employee|g?ta) trainings?\b|"
    # "Workshop: Pronunciation Brush-Up for International TAs".
    r"\bfor (international |new )?(g?tas|teaching assistants|instructors)\s*$|"
    r"\btraining (meeting|weekend)\b|[-–]\s*trainings?\s*$|"
    # Notices rather than events.
    r"\bmove your car\b|\b(football|game ?day) parking\b|\bsurvey\s*$|"
    # "No Bioengineering Seminar Today", "No PAWS today - Fall Break",
    # "Join SOCHI Email List!".
    r"^\s*no [\w ]{1,40}\btoday\b|^\s*join (our |the )?[\w ]{0,30}(email|mailing) list\b|"
    # Team and staff bookings: "VB Visiting Team Practice", "Staff Garden Volunteer Day".
    r"\bvisiting team practice\b|\bvt (practice|shootarounds?)\b|\battendance\s*$|"
    r"^\s*staff (garden|appreciation|holiday party|potluck)\b"
)
# Case-sensitive: a course section ("SWK-S 502 0001", "NURS-B 444", "ANAT-D502")
# or a room code ("Rm IB 317") at the front of a title is a class or a booking.
_INTERNAL_CODES = re.compile(
    r"^\s*[A-Z]{2,5}(-[A-Z]?\s?\d{3}|\s[A-Z]\d{3})[A-Z]?\b|\bRms?\.? [A-Z]{0,3}\s?\d{2,4}\b|"
    # A room booking signed with the booker's lowercase initials:
    # "IUH-Years of Service Awards/Lori Kern/ce", "IUH-MillionMeals/SGirgis/dmg".
    r"/\s*[a-z]{2,4}\s*$|^\s*IU(H|SON|SM)\s*-|"
    # A course number on its own: "Tanner H399 Honors", "SWK ... with 481".
    r"\b[A-Z]\d{3}\b|"
    # A course listed by its number: "FOLK 100: Foundations of our Fields".
    r"^\s*[A-Z]{2,5} [A-Z]?\d{3}[A-Z]?\s*:|"
    # A department's research talk series: "CCBB Seminar Guest Speaker: Dr. ...".
    r"^\s*[A-Z]{2,6} (Seminar|Colloquium)\b|"
    # A class section's booking: "PHYSED 3 017 24692 2268"; an athletics
    # team's practice: "IA VB Practice".
    r"^\s*[A-Z]{3,10} \d+[A-Z]? \d{3} \d{5}\b|^\s*IA [A-Z]{2,3} Practice\s*$|"
    # A department's thesis defense: "CSE Defense: ...", "CSE Dissertation: ...".
    r"^\s*[A-Z]{2,6} (Defen[cs]e|Dissertation|Thesis)\s*[:-]|"
    # A booking held under two initials and nothing else: "MC", "VL".
    r"^\s*[A-Z]{2}\s*$"
)

# --- the source's own category label -----------------------------------------
_SOURCE_CATEGORY = {
    VIRTUAL: _rx(r"\b(online|virtual|webinars?)\b"),
    CONFERENCE: _rx(r"\b(conferences?|symposi(a|ums?))\b"),
    CERTIFICATION: _rx(r"\b(certifications?|continuing education)\b"),
    DEADLINE: _rx(r"\b(deadlines?|academic calendar|important dates|reminders?)\b"),
    INTERNAL: _rx(r"\b(human resources|faculty and staff|staff only|internal)\b"),
    PROFESSIONAL: _rx(r"\b(business|professional development)\b"),
}

# schema.org: an online-only event declares this attendance mode; a hybrid one
# declares MixedEventAttendanceMode and is kept.
_ONLINE_ATTENDANCE_MODE = "OnlineEventAttendanceMode"


# Host -> calendars (first path segment of an event link) that publish room
# bookings and the course schedule rather than events: IU's "campus-center"
# calendar lists "CELL BIOLOGY", "Swim Club Practice", "Ben Wilck 321".
_BOOKING_CALENDARS = {
    # Room bookings and the course schedule (also the Health Sciences Building's:
    # "Simucase Group 4", "Nate"), and IU Human Resources' calendar of employee
    # programs ("Care Talks by Care.com", "Lean and AI 2.0"), Kelley's staff calendar
    # and the faculty teaching academy's ("3 for 30 GenAI Series").
    "events.iu.edu": re.compile(
        r"^(campus-center|hsb|iuhumanrescal|kelley-staff|facet)$", re.IGNORECASE
    ),
}
# Calendars run for alumni rather than students: the IU Alumni Association's
# ("Raclin Legends Honors Night", "IU McKinney Moot Court Finals & Reception").
_ALUMNI_CALENDARS = {
    # a regional alumni chapter's ("NEI Region | Silver Screens Alumni Movie Night"),
    # and Emeriti House's, for retired faculty ("Poetry Writing Group November 2026").
    "events.iu.edu": re.compile(r"^(iu-alumni|emeriti|[a-z]+-alumni)$", re.IGNORECASE),
}


# Paid programs for working professionals: Kelley Executive Education's
# ("Leading with AI: Influence (In-Person)").
_PROFESSIONAL_CALENDARS = {
    "events.iu.edu": re.compile(r"^kelley-execed$", re.IGNORECASE),
}
_LECTURESHIP = _rx(r"\blecture(ship)?s?\b")


def _on_calendar(url: str | None, calendars: dict[str, re.Pattern[str]]) -> bool:
    if not url:
        return False
    parts = urlsplit(url)
    pattern = calendars.get((parts.hostname or "").casefold())
    first = parts.path.strip("/").split("/", 1)[0]
    return bool(pattern and first and pattern.search(first))


def not_attendable_reason(
    *,
    title: str | None,
    description: str | None = None,
    venue: str | None = None,
    address: str | None = None,
    source_category: str | None = None,
    raw: dict[str, Any] | None = None,
    url: str | None = None,
) -> str | None:
    """Why this is not an event someone can turn up to, or None when it is.

    The reason is one of CANCELLED, VIRTUAL, CONFERENCE, CERTIFICATION,
    DEADLINE, INTERNAL, PROFESSIONAL or AUDIENCE, recorded so an excluded event
    can be explained."""
    title = title or ""
    description = description or ""

    if _on_calendar(url, _BOOKING_CALENDARS):
        return INTERNAL
    if _on_calendar(url, _PROFESSIONAL_CALENDARS):
        return PROFESSIONAL
    # A named lecture cross-posted to the alumni calendar is still public.
    if _on_calendar(url, _ALUMNI_CALENDARS) and not _LECTURESHIP.search(title):
        return AUDIENCE

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
        or _WORKING_GROUP_TITLE.search(title)
    ):
        return INTERNAL
    if _CERTIFICATION_TITLE.search(title):
        return CERTIFICATION
    if _SERVICE_TITLE.search(title) or _CLASS_SESSION_TITLE.search(title):
        return INTERNAL
    if _THEME_PERIOD_TITLE.search(title):
        return DEADLINE
    if (
        _AUDIENCE_TITLE.search(title) and not _open_to_all_ages(title)
    ) or _FLOOR_PAIR_TITLE.search(title):
        return AUDIENCE
    if _ACADEMIC_TALK_TITLE.search(title) and not _PERFORMANCE_OR_HANDS_ON.search(title):
        return CONFERENCE
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



def _open_to_all_ages(title: str) -> bool:
    """The title says "all ages" and states no age limit of its own."""
    return bool(_ALL_AGES_TITLE.search(title)) and not _AGE_LIMIT_TITLE.search(title)

def candidate_not_attendable_reason(candidate) -> str | None:
    """not_attendable_reason for an in-flight EventCandidate."""
    return not_attendable_reason(
        title=candidate.title,
        description=candidate.description,
        venue=candidate.venue,
        address=candidate.address,
        source_category=candidate.source_category,
        raw=candidate.raw,
        url=candidate.canonical_url,
    )


def event_not_attendable_reason(event) -> str | None:
    """not_attendable_reason for a stored Event (whose raw record is not kept)."""
    return not_attendable_reason(
        title=event.title,
        description=event.description,
        venue=event.public_venue,
        address=event.public_address,
        source_category=event.source_category,
        url=event.canonical_url,
    )


def hide_unattendable_events(db: Session, *, apply: bool) -> list[tuple[Any, str]]:
    """Find active, unarchived events that are not attendable; deactivate them
    when `apply` is set. Returns (event, reason) for each one found, so a dry
    run can be reviewed first. Used to clean up events imported before the
    import-time filter existed. Reads the events in batches
    (app.services.event_sweeps), so memory stays flat as the table grows."""
    from app.services.event_sweeps import active_event_batches

    found: list[tuple[Any, str]] = []
    for batch in active_event_batches(db):
        for event in batch:
            reason = event_not_attendable_reason(event)
            if reason:
                found.append((event, reason))
                if apply:
                    event.is_active = False
        # Committing each batch keeps its work if the process is stopped part
        # way, and lets the unchanged events of the batch be freed.
        if apply:
            db.commit()
    return found
