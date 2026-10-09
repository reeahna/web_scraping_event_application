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

An admin who sets a geographic filter on a source has said exactly what that
source covers, so a source with one is left to it.
"""

from __future__ import annotations

import re
from typing import Any

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


def address_town(address: str | None) -> tuple[str, str] | None:
    """The (town, state code) an address names, or None when it names none.

    The last "Town, ST" pair wins, since a venue name in front can itself
    contain a comma."""
    if not address:
        return None
    matches = list(_TOWN_STATE.finditer(address.replace("\n", ", ")))
    if not matches:
        return None
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
    if home_state and found_state != home_state:
        return True
    a, b = _simplify(found_town), _simplify(town)
    # "1 Main St Bethlehem, PA" has no comma between street and town, so the
    # town is only the end of what was read.
    last_words = " ".join(a.split()[-len(b.split()):])
    same_town = b in (a, last_words) or (len(b) >= 6 and _within_one_edit(last_words, b))
    return not same_town


def candidate_outside_home_town(candidate, city) -> bool:
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
