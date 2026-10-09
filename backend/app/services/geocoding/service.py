"""Geocoding orchestration: skip rules, cache, status transitions, drain.

Never overwrites administrator corrections or immutable source coordinates —
those events are skipped outright. A result is written to the separate
``geocoded_*`` columns and cached by address hash. The queue is the set of
events in ``pending`` status; the scheduler process drains it in batches.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy import and_, case, or_, select
from sqlalchemy.orm import Session

from app.models.event import Event
from app.models.geocode_cache import GeocodeCache
from app.services.geocoding.provider import GeocodeResult, GeocodingProvider, ProviderUnavailable
from app.services.geocoding.types import address_hash, address_queries, normalize_address

PENDING = "pending"
COMPLETED = "completed"
FAILED = "failed"
SKIPPED = "skipped"
NEEDS_REVIEW = "needs_review"

MAX_AUTOMATIC_ATTEMPTS = 3


def event_locality(event: Event) -> str | None:
    """The event's town as a geocoder-friendly string, e.g.
    "State College, PA, USA", or None when the event has no city."""
    city = event.city
    if city is None:
        return None
    parts = [p for p in (city.name, city.state_or_region, city.country) if p and p.strip()]
    return ", ".join(p.strip() for p in parts) or None


def skip_reason_for(event: Event) -> str | None:
    """Why this event must not be geocoded, or None if it should be. Order
    matters: a protected override and existing source coordinates both win over
    any address we might otherwise look up."""
    if event.latitude is not None and event.longitude is not None:
        return "source_coordinates"
    if event.corrected_latitude is not None or event.corrected_longitude is not None:
        return "protected_override"
    if (
        not address_queries(event.public_address, None)
        and normalize_address(None, event.public_venue) is None
    ):
        return "no_address"
    return None


def _cache_get(db: Session, key: str) -> GeocodeCache | None:
    return db.scalar(select(GeocodeCache).where(GeocodeCache.address_hash == key))


def _cache_put(
    db: Session,
    key: str,
    normalized: str,
    result: GeocodeResult | None,
    provider_name: str,
    now: datetime,
) -> None:
    if _cache_get(db, key) is not None:
        return
    db.add(
        GeocodeCache(
            address_hash=key,
            normalized_address=normalized[:1000],
            found=result is not None,
            latitude=result.latitude if result else None,
            longitude=result.longitude if result else None,
            provider=provider_name,
            display_name=result.display_name if result else None,
            fetched_at=now,
        )
    )


def _apply_result(event: Event, lat: float, lng: float, now: datetime) -> None:
    event.geocoded_latitude = lat
    event.geocoded_longitude = lng
    event.geocode_status = COMPLETED
    event.geocoded_at = now
    event.geocode_last_error = None


# How far around the town centre a venue-name search may look, in degrees
# (about 15 km north-south). Wide enough for a campus and its town, narrow
# enough that "Recital Hall" cannot match one in another state.
_TOWN_BOX_DEGREES = 0.15

# A leading or trailing room/floor part ("Room 101, Linderman Library",
# "Packard Lab 466") that map data never has, so it only stops a match.
_ROOM_PART = re.compile(r"^(room|rm\.?|suite|ste\.?|floor|fl\.?|#)\s*\S+$|\d", re.IGNORECASE)
_TRAILING_ROOM = re.compile(r"\s+(room\s+|rm\.?\s*|#)?\d+[a-z]?$", re.IGNORECASE)


def _without_room(venue: str) -> str:
    parts = [p.strip() for p in venue.split(",") if p.strip()]
    while len(parts) > 1 and _ROOM_PART.search(parts[0]):
        parts = parts[1:]
    return _TRAILING_ROOM.sub("", ", ".join(parts)).strip(" ,") or venue


async def _lookup(
    db: Session,
    provider: GeocodingProvider,
    query: str,
    now: datetime,
    *,
    viewbox: tuple[float, float, float, float] | None = None,
) -> tuple[GeocodeResult | None, bool]:
    """One cached lookup. Returns (result, asked_provider). Raises
    ProviderUnavailable when the provider fails."""
    cache_text = query if viewbox is None else f"{query} @{viewbox[0]:.3f},{viewbox[1]:.3f}"
    key = address_hash(cache_text)
    cached = _cache_get(db, key)
    if cached is not None:
        if cached.found and cached.latitude is not None and cached.longitude is not None:
            return GeocodeResult(cached.latitude, cached.longitude, cached.provider), False
        return None, False
    if viewbox is None:
        result = await provider.geocode(query)
    else:
        result = await provider.geocode(query, viewbox=viewbox)
    _cache_put(db, key, cache_text, result, provider.name, now)
    return result, True


async def _town_box(
    db: Session, event: Event, provider: GeocodingProvider, now: datetime
) -> tuple[float, float, float, float] | None:
    """(south, west, north, east) around the event's town: from the city's own
    centre when an admin set one, otherwise by looking the town up (cached)."""
    city = event.city
    if city is None:
        return None
    lat, lng = city.default_latitude, city.default_longitude
    if lat is None or lng is None:
        locality = event_locality(event)
        if locality is None:
            return None
        found, _ = await _lookup(db, provider, locality, now)
        if found is None:
            return None
        lat, lng = found.latitude, found.longitude
    d = _TOWN_BOX_DEGREES
    return (lat - d, lng - d * 1.3, lat + d, lng + d * 1.3)


async def geocode_event(
    db: Session,
    event: Event,
    provider: GeocodingProvider,
    *,
    now: datetime | None = None,
) -> str:
    """Geocode one event, honouring every skip rule and the cache. Returns the
    resulting status. Never overwrites a correction or source coordinates.

    Scraped locations are rarely a clean postal address, so a few queries are
    tried in turn until one matches: the street address (as written when it
    names its own town, see ``address_queries``), then the venue name searched
    only within the town, then the venue without a room number. Each query's
    answer, hit or miss, is cached on its own.
    """
    now = now or datetime.now(UTC)

    reason = skip_reason_for(event)
    if reason is not None:
        event.geocode_status = SKIPPED
        event.geocode_last_error = reason
        db.commit()
        return SKIPPED

    locality = event_locality(event)
    address = (event.public_address or "").strip() or None
    venue = (event.public_venue or "").strip() or None
    asked = False
    try:
        result = None
        for query in address_queries(address, locality):
            result, asked_now = await _lookup(db, provider, query, now)
            asked = asked or asked_now
            if result is not None:
                break
        if result is None and venue:
            box = await _town_box(db, event, provider, now)
            if box is not None:
                for name in dict.fromkeys([venue, _without_room(venue)]):
                    result, asked_now = await _lookup(db, provider, name, now, viewbox=box)
                    asked = asked or asked_now
                    if result is not None:
                        break
            else:
                query = normalize_address(None, venue, locality)
                result, asked_now = await _lookup(db, provider, query, now)
                asked = asked or asked_now
    except ProviderUnavailable as exc:
        # The provider itself failed — retryable, so leave it recoverable.
        event.geocode_status = FAILED
        event.geocode_attempts += 1
        event.geocode_last_error = str(exc)[:500]
        db.commit()
        return FAILED

    if asked:
        event.geocode_attempts += 1
    if result is None:
        event.geocode_status = NEEDS_REVIEW
        event.geocode_last_error = "no_match" if asked else "no_match_cached"
        db.commit()
        return NEEDS_REVIEW

    _apply_result(event, result.latitude, result.longitude, now)
    db.commit()
    return COMPLETED


def retry_event_geocoding(db: Session, event: Event) -> None:
    """Manual retry: requeue a failed/needs_review event. A skipped event is
    only requeued when the reason no longer applies (so we never re-run a
    protected override)."""
    if event.geocode_status == SKIPPED and skip_reason_for(event) is not None:
        return
    event.geocode_status = PENDING
    event.geocode_last_error = None
    db.commit()


async def drain_geocoding_queue(
    db: Session,
    provider: GeocodingProvider,
    *,
    limit: int = 10,
    now: datetime | None = None,
) -> int:
    """Process up to `limit` pending events. Stops early if the provider goes
    unhealthy mid-batch so we don't hammer a failing service. Returns the count
    processed."""
    if not provider.is_healthy():
        return 0
    # Upcoming events first, soonest first: they are the only ones the public
    # map can show, and a backlog of past events (all pending from before
    # geocoding was switched on) would otherwise keep the map empty for hours.
    today = (now or datetime.now(UTC)).date()
    upcoming = or_(
        and_(Event.end_date.isnot(None), Event.end_date >= today),
        and_(Event.end_date.is_(None), Event.start_date.isnot(None), Event.start_date >= today),
    )
    events = list(
        db.scalars(
            select(Event)
            .where(
                or_(
                    Event.geocode_status == PENDING,
                    # A provider outage is retried a few times on its own.
                    and_(
                        Event.geocode_status == FAILED,
                        Event.geocode_attempts < MAX_AUTOMATIC_ATTEMPTS,
                    ),
                )
            )
            .order_by(
                case((upcoming, 0), else_=1),
                case((Event.geocode_status == PENDING, 0), else_=1),
                Event.start_date.is_(None),
                Event.start_date,
                Event.id,
            )
            .limit(limit)
        )
    )
    processed = 0
    for event in events:
        status = await geocode_event(db, event, provider, now=now)
        processed += 1
        if status == FAILED and not provider.is_healthy():
            break
    return processed
