"""Geocoding providers.

* ``DisabledGeocoder`` — the default. Never makes a request; reports unhealthy.
* ``StaticGeocoder`` — a deterministic in-memory provider for tests. No network.
* ``NominatimGeocoder`` — the real adapter: a descriptive User-Agent, a minimum
  interval between requests (Nominatim's policy is <= 1 req/s), a bounded
  timeout, retries, and a circuit breaker that trips after repeated failures.

A provider returns a ``GeocodeResult`` on a hit, ``None`` on a confident
"no match", and raises ``ProviderUnavailable`` when the provider itself failed
(network/timeout/circuit-open) — the caller treats those three outcomes very
differently (completed / needs_review / failed-and-retryable).
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class ProviderUnavailable(Exception):
    """The provider could not be reached or is in cooldown — retryable."""


@dataclass(frozen=True)
class GeocodeResult:
    latitude: float
    longitude: float
    provider: str
    display_name: str | None = None


@runtime_checkable
class GeocodingProvider(Protocol):
    name: str

    def is_healthy(self) -> bool: ...

    async def geocode(self, address: str) -> GeocodeResult | None: ...


class _CircuitBreaker:
    """Trips after `threshold` consecutive failures; stays open for `cooldown`
    seconds. `clock` is injectable so tests are deterministic."""

    def __init__(self, *, threshold: int, cooldown_seconds: int, clock=_time.monotonic) -> None:
        self._threshold = threshold
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._consecutive_failures = 0
        self._open_until = 0.0

    def is_closed(self) -> bool:
        if self._consecutive_failures < self._threshold:
            return True
        return self._clock() >= self._open_until

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._open_until = 0.0

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._threshold:
            self._open_until = self._clock() + self._cooldown


class DisabledGeocoder:
    name = "disabled"

    def is_healthy(self) -> bool:
        return False

    async def geocode(self, address: str) -> GeocodeResult | None:
        raise ProviderUnavailable("geocoding is disabled")


class StaticGeocoder:
    """Test/double provider. `results` maps a normalized address to a
    GeocodeResult; unknown addresses return None (a confident no-match) unless
    `default` is set. Never touches the network."""

    name = "static"

    def __init__(
        self,
        results: dict[str, GeocodeResult] | None = None,
        *,
        default: GeocodeResult | None = None,
        healthy: bool = True,
    ) -> None:
        self._results = results or {}
        self._default = default
        self._healthy = healthy
        self.calls: list[str] = []

    def is_healthy(self) -> bool:
        return self._healthy

    async def geocode(self, address: str) -> GeocodeResult | None:
        self.calls.append(address)
        if not self._healthy:
            raise ProviderUnavailable("static provider marked unhealthy")
        return self._results.get(address, self._default)


class NominatimGeocoder:
    name = "nominatim"
    _ENDPOINT = "https://nominatim.openstreetmap.org/search"

    def __init__(
        self,
        *,
        user_agent: str,
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
        min_interval_seconds: float = 1.0,
        failure_threshold: int = 5,
        cooldown_seconds: int = 300,
        clock=_time.monotonic,
    ) -> None:
        self._user_agent = user_agent
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._min_interval = min_interval_seconds
        self._clock = clock
        self._last_request_at = 0.0
        self._breaker = _CircuitBreaker(
            threshold=failure_threshold, cooldown_seconds=cooldown_seconds, clock=clock
        )

    def is_healthy(self) -> bool:
        return self._breaker.is_closed()

    async def _respect_rate_limit(self) -> None:
        import asyncio

        elapsed = self._clock() - self._last_request_at
        wait = self._min_interval - elapsed
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_at = self._clock()

    async def geocode(self, address: str) -> GeocodeResult | None:
        if not self._breaker.is_closed():
            raise ProviderUnavailable("nominatim circuit is open")

        import httpx

        last_exc: Exception | None = None
        for _attempt in range(self._max_retries + 1):
            await self._respect_rate_limit()
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.get(
                        self._ENDPOINT,
                        params={"q": address, "format": "json", "limit": 1},
                        headers={"User-Agent": self._user_agent},
                    )
                if response.status_code == 200:
                    payload = response.json()
                    self._breaker.record_success()
                    if not payload:
                        return None  # confident no-match
                    top = payload[0]
                    return GeocodeResult(
                        latitude=float(top["lat"]),
                        longitude=float(top["lon"]),
                        provider=self.name,
                        display_name=top.get("display_name"),
                    )
                # 429/5xx etc. — treat as a provider failure and retry/backoff.
                last_exc = ProviderUnavailable(f"nominatim status {response.status_code}")
            except Exception as exc:  # noqa: BLE001 - network/parse errors are retryable
                last_exc = exc
            self._breaker.record_failure()
        raise ProviderUnavailable(str(last_exc) if last_exc else "nominatim failed")


def get_geocoder(settings) -> GeocodingProvider:
    """Select the provider from settings. Disabled unless explicitly turned on
    with a known provider — so the default configuration makes no live call."""
    if not settings.geocoding_enabled or settings.geocoding_provider == "disabled":
        return DisabledGeocoder()
    if settings.geocoding_provider == "nominatim":
        return NominatimGeocoder(
            user_agent=settings.geocoding_user_agent,
            timeout_seconds=settings.geocoding_timeout_seconds,
            max_retries=settings.geocoding_max_retries,
            min_interval_seconds=settings.geocoding_min_interval_seconds,
            failure_threshold=settings.geocoding_failure_threshold,
            cooldown_seconds=settings.geocoding_cooldown_seconds,
        )
    return DisabledGeocoder()
