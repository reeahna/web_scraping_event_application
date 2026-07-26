"""Asynchronous geocoding (Phase 11).

A provider interface plus a Nominatim adapter, run as a background drain from
the dedicated scheduler process. Disabled by default: no live third-party
request is ever made unless an administrator turns geocoding on. Results are
cached by normalized-address hash, kept separate from immutable source
coordinates, and never overwrite an administrator's correction. Automated
tests inject a static provider — nothing here calls a live service on its own.
"""

from app.services.geocoding.provider import (
    DisabledGeocoder,
    GeocodeResult,
    NominatimGeocoder,
    StaticGeocoder,
    get_geocoder,
)
from app.services.geocoding.service import (
    drain_geocoding_queue,
    geocode_event,
    retry_event_geocoding,
    skip_reason_for,
)

__all__ = [
    "DisabledGeocoder",
    "GeocodeResult",
    "NominatimGeocoder",
    "StaticGeocoder",
    "drain_geocoding_queue",
    "geocode_event",
    "get_geocoder",
    "retry_event_geocoding",
    "skip_reason_for",
]
