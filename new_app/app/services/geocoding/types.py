"""Address normalization and hashing for geocoding."""

from __future__ import annotations

import hashlib
import re

_WS = re.compile(r"\s+")


def normalize_address(address: str | None, venue: str | None) -> str | None:
    """Build a single canonical address string from an event's address and
    venue, or None if there is nothing usable to geocode. Deterministic: the
    same inputs always produce the same string (and therefore the same cache
    key)."""
    parts = [p for p in (address, venue) if p and p.strip()]
    if not parts:
        return None
    combined = ", ".join(_WS.sub(" ", p).strip() for p in parts)
    combined = combined.strip(" ,")
    return combined or None


def address_hash(normalized: str) -> str:
    """A short, stable key for the cache. Case-insensitive so 'Main St' and
    'main st' share a cache entry."""
    return hashlib.sha256(normalized.lower().encode("utf-8")).hexdigest()
