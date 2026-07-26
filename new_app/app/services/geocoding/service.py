"""Geocoding orchestration: skip rules, cache, status transitions, drain.

Never overwrites administrator corrections or immutable source coordinates —
those events are skipped outright. A result is written to the separate
``geocoded_*`` columns and cached by address hash. The queue is the set of
events in ``pending`` status; the scheduler process drains it in batches.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.event import Event
from app.models.geocode_cache import GeocodeCache
from app.services.geocoding.provider import GeocodeResult, GeocodingProvider, ProviderUnavailable
from app.services.geocoding.types import address_hash, normalize_address

PENDING = "pending"
COMPLETED = "completed"
FAILED = "failed"
SKIPPED = "skipped"
NEEDS_REVIEW = "needs_review"


def skip_reason_for(event: Event) -> str | None:
    """Why this event must not be geocoded, or None if it should be. Order
    matters: a protected override and existing source coordinates both win over
    any address we might otherwise look up."""
    if event.latitude is not None and event.longitude is not None:
        return "source_coordinates"
    if event.corrected_latitude is not None or event.corrected_longitude is not None:
        return "protected_override"
    if normalize_address(event.address, event.venue) is None:
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


async def geocode_event(
    db: Session,
    event: Event,
    provider: GeocodingProvider,
    *,
    now: datetime | None = None,
) -> str:
    """Geocode one event, honouring every skip rule and the cache. Returns the
    resulting status. Never overwrites a correction or source coordinates."""
    now = now or datetime.now(UTC)

    reason = skip_reason_for(event)
    if reason is not None:
        event.geocode_status = SKIPPED
        event.geocode_last_error = reason
        db.commit()
        return SKIPPED

    normalized = normalize_address(event.address, event.venue)
    assert normalized is not None  # guaranteed by skip_reason_for
    key = address_hash(normalized)

    cached = _cache_get(db, key)
    if cached is not None:
        if cached.found and cached.latitude is not None and cached.longitude is not None:
            _apply_result(event, cached.latitude, cached.longitude, now)
            db.commit()
            return COMPLETED
        event.geocode_status = NEEDS_REVIEW
        event.geocode_last_error = "no_match_cached"
        db.commit()
        return NEEDS_REVIEW

    try:
        result = await provider.geocode(normalized)
    except ProviderUnavailable as exc:
        # The provider itself failed — retryable, so leave it recoverable.
        event.geocode_status = FAILED
        event.geocode_attempts += 1
        event.geocode_last_error = str(exc)[:500]
        db.commit()
        return FAILED

    event.geocode_attempts += 1
    if result is None:
        _cache_put(db, key, normalized, None, provider.name, now)
        event.geocode_status = NEEDS_REVIEW
        event.geocode_last_error = "no_match"
        db.commit()
        return NEEDS_REVIEW

    _cache_put(db, key, normalized, result, provider.name, now)
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
    events = list(
        db.scalars(
            select(Event).where(Event.geocode_status == PENDING).order_by(Event.id).limit(limit)
        )
    )
    processed = 0
    for event in events:
        status = await geocode_event(db, event, provider, now=now)
        processed += 1
        if status == FAILED and not provider.is_healthy():
            break
    return processed
