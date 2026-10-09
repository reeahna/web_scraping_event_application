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


def address_hash(normalized: str) -> str:
    """A short, stable key for the cache. Case-insensitive so 'Main St' and
    'main st' share a cache entry."""
    return hashlib.sha256(normalized.lower().encode("utf-8")).hexdigest()
