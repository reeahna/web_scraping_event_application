"""Keep each source to the town it was added for.

Eventbrite's city pages ("/d/in--bloomington/all-events/") list everything
within a wide radius, so a Bloomington source brings in Indianapolis, Columbus
and Terre Haute events, and a Bethlehem one brings in Allentown and Easton.
This leaves out an event whose own address names a different town.

It only acts on clear evidence. An address is read for a "Town, ST" pair
("849 S Auto Mall Rd, Bloomington, IN, 47401"); an event with no address, or
one that never names a town and state (a university calendar listing only
"Assembly Hall"), is kept. A one-letter slip in the town's name
("Bloominton") still counts as the town.

A campus calendar that serves several campuses gives no address at all, but
its links name the calendar each event came from; events.iu.edu files IU
Indianapolis, South Bend, Southeast and the other campuses under their own
calendars, so those are left out of an IU Bloomington source by link.

An admin who sets a geographic filter on a source has said exactly what that
source covers, so a source with one is left to it.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

_STATES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas",
    "CA": "california", "CO": "colorado", "CT": "connecticut", "DE": "delaware",
    "DC": "district of columbia", "FL": "florida", "GA": "georgia", "HI": "hawaii",
    "ID": "idaho", "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas",
    "KY": "kentucky", "LA": "louisiana", "ME": "maine", "MD": "maryland",
    "MA": "massachusetts", "MI": "michigan", "MN": "minnesota", "MS": "mississippi",
    "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york",
    "NC": "north carolina", "ND": "north dakota", "OH": "ohio", "OK": "oklahoma",
    "OR": "oregon", "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina",
    "SD": "south dakota", "TN": "tennessee", "TX": "texas", "UT": "utah",
    "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west virginia",
    "WI": "wisconsin", "WY": "wyoming",
}
_STATE_NAMES = {name: abbr for abbr, name in _STATES.items()}

# "Town, ST", "Town, ST 47401", "Town, Indiana, 47401". The town is the last
# comma-separated part before the state.
_STATE_ALTERNATIVES = "|".join(
    sorted([*_STATES, *(n.title() for n in _STATE_NAMES)], key=len, reverse=True)
)
_TOWN_STATE = re.compile(
    rf"(?:^|,)\s*([^,]*?)\s*,\s*({_STATE_ALTERNATIVES})\.?"
    r"(?=\s*(?:,|\d{5}|$|\s*USA?\b|\s*United States\b))",
)


# Host -> the calendars (first path segment of an event link) that belong to a
# different campus town than the one the source was added for.
_OTHER_CAMPUS_CALENDARS = {
    "events.iu.edu": re.compile(
        r"indianapolis|iupui|herron|southeast|southbend|kokomo|northwest|fortwayne|"
        r"columbus|^east$|^ius$|^iusb|^iuk|^iue|^iun",
        re.IGNORECASE,
    ),
}


# Hosts that belong to another campus outright: IU Kokomo's student-life site
# ("cougarlife.kokomo.iu.edu") and the like.
_OTHER_CAMPUS_HOSTS = re.compile(
    r"(^|\.)(indianapolis|southeast|southbend|kokomo|northwest|fortwayne|columbus|east)"
    r"\.iu\.edu$|(^|\.)(iupui|iusb|iun|ius|iue|iuk|pfw)\.edu$"
)


# Host -> titles that name a different campus. Some shared calendars (the
# central "live" one, departments) carry other campuses' events too, and then
# only the title says so: "IU Indy Garden Volunteer Hours".
_OTHER_CAMPUS_TITLES = {
    "events.iu.edu": re.compile(
        r"\b(IU Indy|IU Indianapolis|IUPUI|IUPUC|IU (Southeast|South Bend|Kokomo|Northwest|East|"
        r"Fort Wayne|Columbus)|IUS|IUSB|IUK|IUE|IUN|IUFW)\b"
    ),
}


# Town -> titles that name a different campus of the same university, whatever
# the source: an athletics or esports feed lists every campus's games
# ("Indiana University Kokomo Volleyball vs ...", "IU Indy Men's Soccer vs ...").
_OTHER_CAMPUS_TITLES_BY_TOWN = {
    "bloomington": re.compile(
        r"\b(IU|Indiana University) (Indy|Indianapolis|Southeast|South Bend|Kokomo|Northwest|"
        r"East(?! Asian)|Fort Wayne|Columbus|McKinney)\b|\b(IUPUI|IUPUC|IUK|IUSB|IUN|IUFW)\b"
    ),
}


def names_other_campus(title: str | None, town: str | None) -> bool:
    """True when the title names another campus of the home town's university."""
    pattern = _OTHER_CAMPUS_TITLES_BY_TOWN.get((town or "").casefold())
    return bool(title and pattern and pattern.search(title))


# Town -> its own county. A title that opens with another county is held there:
# "Daviess County Together: A Conversation on ..." on IU's rural-engagement
# calendar, listed for Bloomington.
_HOME_COUNTIES = {
    "bloomington": {"monroe"},
    "iowa city": {"johnson"},
    "bethlehem": {"northampton", "lehigh"},
    "college park": {"prince george's", "prince george’s", "prince georges"},
    "champaign": {"champaign"},
}
_LEADING_COUNTY = re.compile(r"^\s*([A-Z][\w'’.]*(?: [A-Z][\w'’.]*)?) County\b")


def names_other_county(title: str | None, town: str | None) -> bool:
    """True when the title opens with a county other than the home town's."""
    home = _HOME_COUNTIES.get((town or "").casefold())
    match = _LEADING_COUNTY.search(title or "")
    return bool(home and match and match.group(1).casefold() not in home)


def from_other_campus_calendar(url: str | None, title: str | None = None) -> bool:
    """True when an event link sits under another campus's calendar, or its
    title names another campus."""
    if not url:
        return False
    parts = urlsplit(url)
    host = (parts.hostname or "").casefold()
    if _OTHER_CAMPUS_HOSTS.search(host):
        return True
    pattern = _OTHER_CAMPUS_CALENDARS.get(host)
    if pattern is None:
        return False
    first = parts.path.strip("/").split("/", 1)[0]
    if first and pattern.search(first):
        return True
    title_pattern = _OTHER_CAMPUS_TITLES.get(host)
    return bool(title and title_pattern and title_pattern.search(title))


# A school team "at" an opponent is an away game ("Indiana University Women's
# Rowing at Iowa"); "vs" is a home one. A watch party for an away game is in
# town, so it is kept.
_SPORTS = (
    r"football|basketball|soccer|volleyball|baseball|softball|field hockey|ice hockey|hockey|"
    r"rowing|golf|tennis|swimming|diving|wrestling|track|track and field|cross country|"
    r"lacrosse|gymnastics|water polo"
)
_AWAY_GAME = re.compile(
    rf"\b(university|college|IU|state)\b.*?\b({_SPORTS})\s+(at|@)\s",
    re.IGNORECASE,
)
# A tailgate held at the other team's stadium: "IU vs Michigan Away Game Tailgate".
_AWAY_TAILGATE = re.compile(r"\baway game tailgate\b", re.IGNORECASE)
_WATCH_PARTY = re.compile(r"\b(watch|viewing|screening)\b", re.IGNORECASE)

# A team entered in a tournament named for its host: "Indiana University Men's
# Golf Purdue Fall Invitational", "... Golf vs Illini Women's Invitational at
# Medinah". The school's own ("IU Fall Classic", "Indiana Invitational") names
# the school or the town, so one that names neither is away. The team is a
# "Men's"/"Women's" one, or baseball or softball: "Lions Club Golf Classic"
# names no school and "State Street Golf Classic" no team, and football has no
# invitationals ("Football vs Purdue ... Classic" is a home game).
_SCHOOL_SPORT = re.compile(
    rf"^(?P<school>.*?\b(university|college|IU|state)\b.*?)"
    rf"(\b(men|women)['\u2019]?s\s+({_SPORTS})|\b(baseball|softball))\b(?P<rest>.*)$",
    re.IGNORECASE,
)
_HOSTED_TOURNAMENT = re.compile(
    r"\b(invitational|classic|intercollegiate|shootout)\b", re.IGNORECASE
)
# Golf tournaments also go by "Tradition": "Women's Golf vs Landfall Tradition".
_GOLF_TOURNAMENT = re.compile(r"\bgolf\b.*\btradition\b", re.IGNORECASE)
_NOT_A_NAME = {
    "university", "college", "of", "the", "and", "at", "men", "mens", "women", "womens",
    "s", "am", "pm",
}


def _words(text: str) -> set[str]:
    words = re.findall(r"[a-z]+", text.casefold().replace("\u2019", "").replace("'", ""))
    return {w for w in words if w not in _NOT_A_NAME}


def _home_names(school: str, town: str | None) -> set[str]:
    """Words that name the home side: the school's own ("indiana"), its
    initials ("iu") and the town's."""
    names = _words(school)
    initials = "".join(w[0] for w in re.findall(r"\b[A-Z][a-z]+\b", school)
                       if w.casefold() not in {"men", "mens", "women", "womens", "the", "of"})
    if len(initials) >= 2:
        names.add(initials.casefold())
    if town:
        names |= _words(town)
    return names


def _at_other_schools_tournament(title: str, town: str | None) -> bool:
    match = _SCHOOL_SPORT.search(title)
    if match is None or not (
        _HOSTED_TOURNAMENT.search(match.group("rest")) or _GOLF_TOURNAMENT.search(title)
    ):
        return False
    return not (_words(match.group("rest")) & _home_names(match.group("school"), town))


# A home-town team "at" somewhere, named by the town rather than "University":
# "Iowa Ice Hockey at Nebraska" in Iowa City. "Pickup Basketball at the SRSC"
# names no town, so it stays.
_TOWN_TEAM_AT = re.compile(
    rf"^(?P<team>.*?)\b({_SPORTS})\s+(at|@)\s+(?!the\b)", re.IGNORECASE
)


def _town_team_away(title: str, town: str | None) -> bool:
    match = _TOWN_TEAM_AT.search(title)
    if match is None or not town:
        return False
    return bool(_words(match.group("team")) & _words(town))


# A regatta named for another river: "Lehigh University Men's Rowing vs Head of
# the Schuylkill". One named for the home town's own river names the school or
# the town ("Head of the Lehigh"), so it stays.
_HEAD_OF_THE = re.compile(
    r"^(?P<school>.*?)\browing\b.*\bhead of the (?P<river>[\w ]+?)\s*$", re.IGNORECASE
)


def _away_regatta(title: str, town: str | None) -> bool:
    match = _HEAD_OF_THE.search(title)
    if match is None:
        return False
    return not (_words(match.group("river")) & _home_names(match.group("school"), town))


def is_away_game(title: str | None, town: str | None = None) -> bool:
    if not title or _WATCH_PARTY.search(title):
        return False
    return (
        bool(_AWAY_GAME.search(title) or _AWAY_TAILGATE.search(title))
        or _at_other_schools_tournament(title, town)
        or _town_team_away(title, town)
        or _away_regatta(title, town)
    )


def _state_code(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().rstrip(".")
    if value.upper() in _STATES:
        return value.upper()
    return _STATE_NAMES.get(value.casefold())


def _simplify(town: str) -> str:
    town = town.casefold().replace(".", "")
    town = re.sub(r"\bsaint\b", "st", town)
    return " ".join(town.split())


def _within_one_edit(a: str, b: str) -> bool:
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = j = edits = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        edits += 1
        if edits > 1:
            return False
        if len(a) == len(b):
            i += 1
        j += 1
    return edits + (len(b) - j) <= 1


# Words that end a street, room or building name rather than a town.
_NOT_A_TOWN = re.compile(
    r"\b(st|street|ave|avenue|rd|road|dr|drive|blvd|boulevard|ln|lane|way|pl|place|ct|court|"
    r"pkwy|parkway|hwy|highway|pike|trail|circle|sq|square|room|rm|suite|ste|floor|fl|hall|"
    r"building|bldg|center|centre|auditorium|theater|theatre|campus|park|lobby|gallery)\.?$",
    re.IGNORECASE,
)


_UNIT = re.compile(r"(suite|ste|unit|apt|#|floor|fl|room|rm)\.?\s*#?\s*\w{1,5}", re.IGNORECASE)


def _trailing_town(address: str) -> str | None:
    """'Lawrence County Courthouse Square 1005 15th St, Bedford': a street
    with a number, then a part that is only a name. Local listings often
    stop at the town and leave the state off."""
    parts = [p.strip() for p in address.split(",") if p.strip()]
    # "1140 IN-135, Greenwood, 46142": a ZIP code after the town, no state.
    if len(parts) >= 3 and re.fullmatch(r"\d{5}(-\d{4})?", parts[-1]):
        parts = parts[:-1]
    # "1395 Jordan St, Suite B, North Liberty": a suite or unit between.
    if len(parts) >= 3 and _UNIT.fullmatch(parts[-2]):
        del parts[-2]
    if len(parts) < 2 or not re.search(r"\d", parts[-2]):
        return None
    last = parts[-1]
    if not re.fullmatch(r"[A-Za-z][A-Za-z .'\-]*", last) or _NOT_A_TOWN.search(last):
        return None
    return last


def address_town(address: str | None) -> tuple[str, str | None] | None:
    """The (town, state code) an address names, or None when it names none.
    The state is None when the address gives a town but no state.

    The last "Town, ST" pair wins, since a venue name in front can itself
    contain a comma."""
    if not address:
        return None
    # "Indianapolis,, IN" and "(Delaware Township), NJ" as some listings write them.
    address = re.sub(r"[()]", "", re.sub(r",\s*(,\s*)+", ", ", address.replace("\n", ", ")))
    matches = list(_TOWN_STATE.finditer(address))
    if not matches:
        town = _trailing_town(address)
        return (town, None) if town else None
    town, state = matches[-1].group(1), matches[-1].group(2)
    # "123 Main St Bloomington" without a comma before the town: take the
    # trailing words that are not a street.
    town = re.sub(r"^.*\d\S*\s+", "", town).strip()
    code = _state_code(state)
    if not re.fullmatch(r"[A-Za-z][A-Za-z .'\-]*", town) or code is None:
        return None
    return town, code


def is_outside_home_town(
    *, address: str | None, venue: str | None, town: str | None, state: str | None
) -> bool:
    """True only when the event's own address names a town other than `town`
    (or the same town name in another state)."""
    if not town:
        return False
    found = address_town(address) or address_town(venue)
    if found is None:
        return False
    found_town, found_state = found
    home_state = _state_code(state)
    if home_state and found_state and found_state != home_state:
        return True
    a, b = _simplify(found_town), _simplify(town)
    # "1 Main St Bethlehem, PA" has no comma between street and town, so the
    # town is only the end of what was read.
    last_words = " ".join(a.split()[-len(b.split()):])
    same_town = b in (a, last_words) or (len(b) >= 6 and _within_one_edit(last_words, b))
    return not same_town


# Another school's club tournament a team travels to: "University of Loyola
# Chicago Club Softball Series", "Drake University Mock Trial Tournament".
_SCHOOL_HOSTED = re.compile(
    r"^\s*(?P<school>university of [\w .&'-]+?|[\w .&'-]+? (university|college))\b"
    r".*\b(tournament|invitational|regatta|classic|club \w+ series)\s*$",
    re.IGNORECASE,
)


def at_other_school_event(title: str | None, city) -> bool:
    """True when the title opens with a school that is not one of the town's
    own (as listed on the city) and names a tournament it hosts. A town with
    no schools listed is left alone."""
    if not title or city is None:
        return False
    match = _SCHOOL_HOSTED.search(title)
    universities = getattr(city, "universities", None) or []
    if match is None or not universities:
        return False
    home = _words(city.name or "")
    for university in universities:
        home |= _home_names(university.name, None)
    return not (_words(match.group("school")) & home)


def candidate_outside_home_town(candidate, city) -> bool:
    if is_away_game(candidate.title, city.name if city is not None else None):
        return True
    if from_other_campus_calendar(candidate.canonical_url, candidate.title):
        return True
    if names_other_campus(candidate.title, city.name if city is not None else None):
        return True
    if names_other_county(candidate.title, city.name if city is not None else None):
        return True
    if at_other_school_event(candidate.title, city):
        return True
    if city is None:
        return False
    return is_outside_home_town(
        address=candidate.address,
        venue=candidate.venue,
        town=city.name,
        state=city.state_or_region,
    )


def event_outside_home_town(event) -> bool:
    city = event.city
    if is_away_game(event.title, city.name if city is not None else None):
        return True
    if from_other_campus_calendar(event.canonical_url, event.title):
        return True
    if names_other_campus(event.title, city.name if city is not None else None):
        return True
    if names_other_county(event.title, city.name if city is not None else None):
        return True
    if at_other_school_event(event.title, city):
        return True
    if city is None:
        return False
    return is_outside_home_town(
        address=event.public_address,
        venue=event.public_venue,
        town=city.name,
        state=city.state_or_region,
    )


def source_has_own_geographic_filter(website) -> bool:
    config = website.approved_pattern or {}
    geo = config.get("geographic_filters") if isinstance(config, dict) else None
    if not geo:
        return False
    from app.schemas.geographic import GeographicFilterConfig

    try:
        return GeographicFilterConfig.model_validate(geo).has_any_rule()
    except ValueError:
        return False


def hide_out_of_town_events(db: Session, *, apply: bool) -> list[Any]:
    """Active events whose address names another town; deactivated when
    `apply` is set. Sources with their own geographic filter are skipped.
    Clears what was imported before the import-time check existed."""
    from app.models.event import Event

    found: list[Any] = []
    query = db.query(Event).filter(Event.is_active.is_(True), Event.archived_at.is_(None))
    for event in query.order_by(Event.id):
        if event.website is not None and source_has_own_geographic_filter(event.website):
            continue
        if event_outside_home_town(event):
            found.append(event)
            if apply:
                event.is_active = False
    if apply:
        db.commit()
    return found
