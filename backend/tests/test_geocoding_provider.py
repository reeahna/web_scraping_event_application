"""Phase 11: geocoding provider adapters and helpers (no network)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services.geocoding.provider import (
    DisabledGeocoder,
    GeocodeResult,
    NominatimGeocoder,
    ProviderUnavailable,
    StaticGeocoder,
    _CircuitBreaker,
    get_geocoder,
)
from app.services.geocoding.types import address_hash, address_queries, normalize_address


def test_normalize_address_combines_and_is_deterministic():
    a = normalize_address("  100 Main   St ", "The Hall")
    b = normalize_address("100 Main St", "The Hall")
    assert a == "100 Main St, The Hall"
    assert a == b
    assert normalize_address(None, None) is None
    assert normalize_address("   ", "") is None


def test_normalize_address_adds_the_town_unless_already_named():
    # A venue alone would geocode to any same-named place in the world.
    assert normalize_address(None, "HUB", "State College, PA") == "HUB, State College, PA"
    assert (
        normalize_address("100 College Ave, State College", None, "State College, PA")
        == "100 College Ave, State College"
    )
    # The town alone is not a location.
    assert normalize_address(None, None, "State College, PA") is None


def test_address_hash_is_case_insensitive():
    assert address_hash("100 Main St") == address_hash("100 MAIN st")


def test_disabled_geocoder_never_calls():
    geo = DisabledGeocoder()
    assert geo.is_healthy() is False
    with pytest.raises(ProviderUnavailable):
        asyncio.run(geo.geocode("anywhere"))


def test_static_geocoder_returns_and_records():
    geo = StaticGeocoder(default=GeocodeResult(1.0, 2.0, "static"))
    result = asyncio.run(geo.geocode("x"))
    assert result.latitude == 1.0
    assert geo.calls == ["x"]


def test_circuit_breaker_trips_and_recovers():
    now = [0.0]
    breaker = _CircuitBreaker(threshold=2, cooldown_seconds=100, clock=lambda: now[0])
    assert breaker.is_closed() is True
    breaker.record_failure()
    assert breaker.is_closed() is True  # one failure, still under threshold
    breaker.record_failure()
    assert breaker.is_closed() is False  # tripped
    now[0] = 101.0
    assert breaker.is_closed() is True  # cooldown elapsed
    breaker.record_success()
    assert breaker.is_closed() is True


def test_get_geocoder_defaults_to_disabled():
    disabled = SimpleNamespace(geocoding_enabled=False, geocoding_provider="nominatim")
    assert isinstance(get_geocoder(disabled), DisabledGeocoder)

    unknown = SimpleNamespace(geocoding_enabled=True, geocoding_provider="mystery")
    assert isinstance(get_geocoder(unknown), DisabledGeocoder)


def test_get_geocoder_builds_nominatim_when_enabled():
    settings = SimpleNamespace(
        geocoding_enabled=True,
        geocoding_provider="nominatim",
        geocoding_user_agent="ua",
        geocoding_timeout_seconds=5.0,
        geocoding_max_retries=1,
        geocoding_min_interval_seconds=1.0,
        geocoding_failure_threshold=3,
        geocoding_cooldown_seconds=60,
    )
    geo = get_geocoder(settings)
    assert isinstance(geo, NominatimGeocoder)
    assert geo.name == "nominatim"


def test_address_with_its_own_town_is_searched_as_written():
    # A nearby town's address must not get the event's town tacked on.
    assert address_queries("19 s 4th street, Emmaus, PA, 18049", "Bethlehem, PA") == [
        "19 s 4th street, Emmaus, PA",
        "19 s 4th street, Emmaus",
    ]


def test_address_drops_floors_and_placeholders():
    assert address_queries(
        "One American Square, 36th Floor, Indianapolis, IN, 46282", "Bloomington, IN"
    )[0] == "One American Square, Indianapolis, IN"
    assert address_queries("TBA, Bloomington, IN, TBA", "Bloomington, IN") == []
    assert address_queries("   ", "Bloomington, IN") == []


def test_bare_street_gets_the_events_town():
    assert address_queries("100 Main St", "Bethlehem, PA") == ["100 Main St, Bethlehem, PA"]
