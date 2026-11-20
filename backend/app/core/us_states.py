"""US state names, for showing a town's state the same way on every page.

Towns are entered both ways: `data/college_towns/cities.csv` uses postal
abbreviations ("MI") while towns edited in the admin often carry the full name
("Indiana"). Public pages show the full name, so a title reads "Ann Arbor,
Michigan" whichever way the town was entered. Stored values are never changed.
"""

from __future__ import annotations

US_STATES: dict[str, str] = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "DC": "District of Columbia",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "PR": "Puerto Rico",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}

_BY_NAME = {name.lower(): name for name in US_STATES.values()}


def state_name(value: str | None) -> str | None:
    """The full name for a postal abbreviation or a full name in any case;
    anything else (a non-US region) is returned as entered, trimmed."""
    if not value or not value.strip():
        return None
    cleaned = value.strip()
    return US_STATES.get(cleaned.upper().rstrip(".")) or _BY_NAME.get(cleaned.lower(), cleaned)


def is_us_state(value: str | None) -> bool:
    if not value:
        return False
    cleaned = value.strip()
    return cleaned.upper().rstrip(".") in US_STATES or cleaned.lower() in _BY_NAME
