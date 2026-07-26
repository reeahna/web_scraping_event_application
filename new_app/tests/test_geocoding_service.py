"""Phase 11: geocoding orchestration — skip rules, cache, states, drain."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from app.models.geocode_cache import GeocodeCache
from app.services.geocoding.provider import GeocodeResult, StaticGeocoder
from app.services.geocoding.service import (
    drain_geocoding_queue,
    geocode_event,
    retry_event_geocoding,
    skip_reason_for,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
_HIT = GeocodeResult(39.8, -89.6, "static", display_name="Somewhere")


def _geocode(db, event, provider):
    return asyncio.run(geocode_event(db, event, provider, now=NOW))


def test_source_coordinates_are_skipped(make_city, make_event, db_session):
    city = make_city()
    event = make_event(city, address="100 Main St", latitude=1.0, longitude=2.0)
    assert skip_reason_for(event) == "source_coordinates"
    status = _geocode(db_session, event, StaticGeocoder(default=_HIT))
    assert status == "skipped"
    assert event.geocoded_latitude is None


def test_administrator_correction_is_protected(make_city, make_event, db_session):
    city = make_city()
    event = make_event(city, address="100 Main St", corrected_latitude=5.0, corrected_longitude=6.0)
    assert skip_reason_for(event) == "protected_override"
    provider = StaticGeocoder(default=_HIT)
    assert _geocode(db_session, event, provider) == "skipped"
    assert provider.calls == []  # never even queried
    assert event.geocoded_latitude is None


def test_event_without_address_is_skipped(make_city, make_event, db_session):
    city = make_city()
    event = make_event(city, address=None, venue=None)
    assert _geocode(db_session, event, StaticGeocoder(default=_HIT)) == "skipped"


def test_successful_geocode_writes_derived_columns_only(make_city, make_event, db_session):
    city = make_city()
    event = make_event(city, address="100 Main St", venue="The Hall")
    status = _geocode(db_session, event, StaticGeocoder(default=_HIT))
    assert status == "completed"
    assert event.geocoded_latitude == 39.8
    assert event.geocoded_longitude == -89.6
    # Immutable source coordinates remain empty; public prefers the geocode.
    assert event.latitude is None
    assert event.public_latitude == 39.8
    assert event.geocoded_at is not None


def test_cache_prevents_a_second_provider_call(make_city, make_event, db_session):
    city = make_city()
    provider = StaticGeocoder(default=_HIT)
    a = make_event(city, address="100 Main St", venue="Hall", canonical_url="https://x/a")
    b = make_event(city, address="100 Main St", venue="Hall", canonical_url="https://x/b")
    assert _geocode(db_session, a, provider) == "completed"
    assert _geocode(db_session, b, provider) == "completed"
    assert len(provider.calls) == 1  # second resolved from cache
    assert db_session.query(GeocodeCache).count() == 1


def test_no_match_is_needs_review_and_cached(make_city, make_event, db_session):
    city = make_city()
    provider = StaticGeocoder(default=None)  # confident no-match
    event = make_event(city, address="Nowhere at all")
    assert _geocode(db_session, event, provider) == "needs_review"
    cache = db_session.query(GeocodeCache).one()
    assert cache.found is False


def test_provider_failure_is_failed_and_retryable(make_city, make_event, db_session):
    city = make_city()
    provider = StaticGeocoder(healthy=False)  # raises ProviderUnavailable
    event = make_event(city, address="100 Main St")
    assert _geocode(db_session, event, provider) == "failed"
    assert event.geocode_attempts == 1

    # Manual retry requeues it; a now-healthy provider completes it.
    retry_event_geocoding(db_session, event)
    assert event.geocode_status == "pending"
    assert _geocode(db_session, event, StaticGeocoder(default=_HIT)) == "completed"


def test_retry_does_not_requeue_a_protected_skip(make_city, make_event, db_session):
    city = make_city()
    event = make_event(city, address="X", corrected_latitude=1.0)
    _geocode(db_session, event, StaticGeocoder(default=_HIT))
    assert event.geocode_status == "skipped"
    retry_event_geocoding(db_session, event)
    assert event.geocode_status == "skipped"  # still protected


def test_drain_processes_pending_and_respects_disabled(make_city, make_event, db_session):
    from app.services.geocoding.provider import DisabledGeocoder

    city = make_city()
    make_event(city, address="1 A St", canonical_url="https://x/1")
    make_event(city, address="2 B St", canonical_url="https://x/2")
    # Disabled provider is unhealthy -> nothing processed, no calls.
    assert asyncio.run(drain_geocoding_queue(db_session, DisabledGeocoder(), now=NOW)) == 0

    provider = StaticGeocoder(default=_HIT)
    processed = asyncio.run(drain_geocoding_queue(db_session, provider, limit=10, now=NOW))
    assert processed == 2
