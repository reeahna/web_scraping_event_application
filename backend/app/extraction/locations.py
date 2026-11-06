"""Reading a venue name and address out of a schema.org `location`.

Event markup publishes `location` in several shapes, all valid schema.org:

* a Place object: `{"@type": "Place", "name": "...", "address": {...}}`
* a bare string: `"location": "Memorial Hall"`
* a list mixing a Place with a VirtualLocation (hybrid events), or several
  Places;
* a Place with only an address and no name.

A plain `location.name` lookup only handles the first, so events from pages
using the others were stored with no venue and shown as "Venue TBD".
"""

from __future__ import annotations

import re
from typing import Any

from app.extraction.sanitize import strip_to_text

# Placeholders a source writes when it doesn't know the venue yet. They mean
# "no venue", so an event carrying one is still looked up on its own page.
_PLACEHOLDER_RE = re.compile(
    r"^\s*(tba|tbd|tbc|n/?a|none|to be (announced|determined|confirmed)|location tbd|"
    r"venue tbd|venue tba|location tba)\.?\s*$",
    re.IGNORECASE,
)

_MAX_VENUE_LENGTH = 200


def is_placeholder_venue(value: Any) -> bool:
    return isinstance(value, str) and bool(_PLACEHOLDER_RE.match(value))


def has_venue(raw: dict[str, Any]) -> bool:
    value = raw.get("venue")
    if not isinstance(value, str):
        return False
    text = strip_to_text(value)
    return bool(text) and not is_placeholder_venue(text)


def clean_venue(value: Any, title: str | None = None) -> str | None:
    """A usable venue name, or None for empty, placeholder, overlong (a
    paragraph, not a name) or title-repeating values."""
    if not isinstance(value, str):
        return None
    text = strip_to_text(value)
    if not text or len(text) < 2 or len(text) > _MAX_VENUE_LENGTH:
        return None
    if is_placeholder_venue(text):
        return None
    if title and text.casefold() == title.strip().casefold():
        return None
    return text


def address_text(value: Any) -> str | None:
    """A PostalAddress (or plain string) as one line."""
    if isinstance(value, str):
        return strip_to_text(value) or None
    if isinstance(value, dict):
        parts = [
            value.get("streetAddress"),
            value.get("addressLocality"),
            value.get("addressRegion"),
            value.get("postalCode"),
        ]
        texts = (str(p).strip() for p in parts if isinstance(p, str | int))
        joined = ", ".join(t for t in texts if t)
        return joined or None
    if isinstance(value, list) and value:
        return address_text(value[0])
    return None


def _is_virtual(node: dict[str, Any]) -> bool:
    types = node.get("@type")
    if isinstance(types, str):
        types = [types]
    return isinstance(types, list) and any("virtuallocation" in str(t).lower() for t in types)


def venue_from_location(value: Any) -> tuple[str | None, str | None]:
    """(venue name, address) from a schema.org `location` of any shape. A
    VirtualLocation is not a venue; a Place with only an address yields
    (None, address)."""
    if isinstance(value, str):
        return clean_venue(value), None
    if isinstance(value, list):
        address: str | None = None
        for item in value:
            venue, item_address = venue_from_location(item)
            if venue:
                return venue, item_address
            address = address or item_address
        return None, address
    if isinstance(value, dict):
        if _is_virtual(value):
            return None, None
        return clean_venue(value.get("name")), address_text(value.get("address"))
    return None, None
