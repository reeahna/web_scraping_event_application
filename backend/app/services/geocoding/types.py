"""Address normalization and hashing for geocoding."""

from __future__ import annotations

import hashlib
import re

_WS = re.compile(r"\s+")


def normalize_address(
    address: str | None, venue: str | None, locality: str | None = None
) -> str | None:
    """Build a single canonical address string from an event's address and
    venue, or None if there is nothing usable to geocode. Deterministic: the
    same inputs always produce the same string (and therefore the same cache
    key).

    `locality` is the event's town ("State College, PA, USA"). Scraped
    addresses are often just a venue or a street ("HUB", "100 Main St"), which
    a geocoder resolves to whichever match it likes anywhere in the world, so
    the town is appended unless the address already names it. It never makes
    an empty address geocodable on its own: the town alone is not a location.
    """
    parts = [p for p in (address, venue) if p and p.strip()]
    if not parts:
        return None
    if locality and locality.strip():
        town = locality.split(",")[0].strip().lower()
        if town and not any(town in p.lower() for p in parts):
            parts.append(locality)
    combined = ", ".join(_WS.sub(" ", p).strip() for p in parts)
    combined = combined.strip(" ,")
    return combined or None


_ZIP = re.compile(r"\s*\b\d{5}(?:-\d{4})?$")
_FILLER = re.compile(r"^(tba|tbd|n/?a|none|online|virtual)$", re.IGNORECASE)
_UNIT = re.compile(
    r"^(\d+(st|nd|rd|th)\s+floor|floor\s+\S+|suite\s+\S+|ste\.?\s+\S+|room\s+\S+|#\s*\S+)$",
    re.IGNORECASE,
)


def address_queries(address: str | None, locality: str | None) -> list[str]:
    """Search strings to try, best first, for a scraped street address.

    An address that already carries its own town ("19 S 4th St, Emmaus, PA,
    18049") is searched as written, never with the event's town tacked on:
    many listings are for nearby towns, and "Emmaus ..., Bethlehem, PA" matches
    nothing. Zip codes, floor/suite parts and placeholders like "TBA" are
    dropped, since a geocoder fails the whole query on any part it lacks. A
    bare street ("100 Main St") gets the event's town added.
    """
    if not address:
        return []
    parts = [p for p in (_WS.sub(" ", p).strip() for p in address.split(",")) if p]
    if not parts or _FILLER.match(parts[0]):
        # "TBA, Bloomington, IN": only the town is known, which is no location.
        return []
    parts = [p for p in parts if not _FILLER.match(p)]
    cleaned = [p for p in (_ZIP.sub("", p).strip() for p in parts) if p]
    cleaned = [p for p in cleaned if not _UNIT.match(p)] or cleaned
    if not any(ch.isalnum() for ch in "".join(cleaned)):
        return []
    if len(cleaned) == 1:
        query = normalize_address(cleaned[0], None, locality)
        return [query] if query else []
    queries = [", ".join(cleaned)]
    if len(cleaned) > 2:
        # Street and town alone, in case the state is spelled oddly.
        queries.append(", ".join(cleaned[:2]))
    return list(dict.fromkeys(queries))


def address_hash(normalized: str) -> str:
    """A short, stable key for the cache. Case-insensitive so 'Main St' and
    'main st' share a cache entry."""
    return hashlib.sha256(normalized.lower().encode("utf-8")).hexdigest()
