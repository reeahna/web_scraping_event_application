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


# Failed logins. Two counters, so neither a single address guessing at many
# accounts nor many addresses guessing at one account gets unlimited tries.
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_FAILURES_PER_ACCOUNT = 10
LOGIN_FAILURES_PER_IP = 30


def _login_keys(email: str, ip_address: str | None) -> list[tuple[str, int]]:
    keys = [(f"login-account:{email}", LOGIN_FAILURES_PER_ACCOUNT)]
    if ip_address:
        keys.append((f"login-ip:{ip_address}", LOGIN_FAILURES_PER_IP))
    return keys


def login_is_locked(email: str, ip_address: str | None) -> bool:
    """True when this account or this address has used up its failed attempts
    for the window. Checked before the password, so a locked account cannot be
    probed further, even with the right password."""
    backend = get_rate_limit_backend()
    return any(
        backend.count(key, window_seconds=LOGIN_WINDOW_SECONDS) >= limit
        for key, limit in _login_keys(email, ip_address)
    )


def record_login_failure(email: str, ip_address: str | None) -> None:
    backend = get_rate_limit_backend()
    for key, limit in _login_keys(email, ip_address):
        backend.allow(key, limit=limit, window_seconds=LOGIN_WINDOW_SECONDS)


def clear_login_failures(email: str) -> None:
    """A successful login clears the account's count (not the address's, which
    would let one good login reset a guessing run against other accounts)."""
    get_rate_limit_backend().reset(f"login-account:{email}")
