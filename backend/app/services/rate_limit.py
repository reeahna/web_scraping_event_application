"""Rate limiting with a pluggable backend.

The default backend is an in-process counter — a **development-safe placeholder**
that is not shared across workers/instances and resets on restart. For
production, set `RATE_LIMIT_BACKEND=redis` and `REDIS_URL`; the Redis backend is
a shared fixed-window counter that works across every web worker and instance
(and is why `/health/ready` flags the in-process limiter as a blocker).

Both backends implement the same `RateLimitBackend.allow(...)` interface, and
`sign_in_start_allowed` is what callers use; it limits how often one address
can start a social sign-in.
"""

import time
from collections import defaultdict
from typing import Protocol, runtime_checkable

from app.config import get_settings

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

    def count(self, key: str, *, window_seconds: int) -> int:
        """How many attempts `key` has in the window, without recording one."""
        ...

    def reset(self, key: str) -> None:
        """Forget every attempt recorded for `key`."""
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

    def count(self, key: str, *, window_seconds: int) -> int:
        now = time.monotonic()
        attempts = _attempts_by_ip.get(key, [])
        return sum(1 for t in attempts if now - t < window_seconds)

    def reset(self, key: str) -> None:
        _attempts_by_ip.pop(key, None)


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

    def count(self, key: str, *, window_seconds: int) -> int:
        return int(self._client.get(f"{self._namespace}:{key}") or 0)

    def reset(self, key: str) -> None:
        self._client.delete(f"{self._namespace}:{key}")


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


SIGN_IN_WINDOW_SECONDS = 15 * 60


def sign_in_start_allowed(ip_address: str | None) -> bool:
    """Record a sign-in start from this address and say whether it is within
    the limit. Each start stores a one-time state record, so this keeps one
    address from filling that table."""
    if not ip_address:
        return True
    settings = get_settings()
    return get_rate_limit_backend(settings).allow(
        f"sign-in-start:{ip_address}",
        limit=settings.sign_in_starts_per_15_minutes,
        window_seconds=SIGN_IN_WINDOW_SECONDS,
    )
