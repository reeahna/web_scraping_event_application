"""Rate limiting with a pluggable backend.

The default backend is an in-process counter — a **development-safe placeholder**
that is not shared across workers/instances and resets on restart. For
production, set `RATE_LIMIT_BACKEND=redis` and `REDIS_URL`; the Redis backend is
a shared fixed-window counter that works across every web worker and instance
(and is why `/health/ready` flags the in-process limiter as a blocker).

Both backends implement the same `RateLimitBackend.allow(...)` interface, and
`check_registration_rate_limit` is unchanged from the caller's perspective —
`app.routers.registration` still calls it the same way.
"""

import time
from collections import defaultdict
from typing import Protocol, runtime_checkable

from app.config import get_settings
from app.core.exceptions import AppError

_WINDOW_SECONDS = 3600

# Module-level so the in-memory backend's state is process-global and the test
# suite can reset it between tests (see tests/conftest.py).
_attempts_by_ip: dict[str, list[float]] = defaultdict(list)


@runtime_checkable
class RateLimitBackend(Protocol):
    name: str

    def allow(self, key: str, *, limit: int, window_seconds: int) -> bool:
        """Record an attempt for `key` and return True if it is within `limit`
        for the window, False if the limit is exceeded."""
        ...


class InMemoryRateLimitBackend:
    """Sliding-window counter in a process-local dict. Not shared across
    workers/instances — development only."""

    name = "memory"

    def allow(self, key: str, *, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        attempts = _attempts_by_ip[key]
        attempts[:] = [t for t in attempts if now - t < window_seconds]
        if len(attempts) >= limit:
            return False
        attempts.append(now)
        return True


class RedisRateLimitBackend:
    """Shared fixed-window counter in Redis (INCR + EXPIRE). Works across every
    worker and instance. The client is injected so it can be exercised with a
    fake Redis in tests and built from `REDIS_URL` in production."""

    name = "redis"

    def __init__(self, client, *, namespace: str = "ratelimit") -> None:
        self._client = client
        self._namespace = namespace

    def allow(self, key: str, *, limit: int, window_seconds: int) -> bool:
        redis_key = f"{self._namespace}:{key}"
        count = self._client.incr(redis_key)
        if count == 1:
            self._client.expire(redis_key, window_seconds)
        return count <= limit


_backend: RateLimitBackend | None = None


def get_rate_limit_backend(settings=None) -> RateLimitBackend:
    global _backend
    settings = settings or get_settings()
    if settings.rate_limit_backend == "redis" and settings.redis_url:
        if not isinstance(_backend, RedisRateLimitBackend):
            import redis  # imported lazily so dev/tests need no Redis server

            _backend = RedisRateLimitBackend(redis.from_url(settings.redis_url))
        return _backend
    if not isinstance(_backend, InMemoryRateLimitBackend):
        _backend = InMemoryRateLimitBackend()
    return _backend


def check_registration_rate_limit(ip_address: str | None) -> None:
    """Raise AppError(429) if this IP has attempted registration too many times
    in the last hour, via the configured backend."""
    if not ip_address:
        return
    settings = get_settings()
    backend = get_rate_limit_backend(settings)
    if not backend.allow(
        ip_address, limit=settings.registration_rate_limit_per_hour,
        window_seconds=_WINDOW_SECONDS,
    ):
        raise AppError(
            "Too many registration attempts from this address. Please try again later.",
            status_code=429,
        )
