"""Rate-limit backends: in-memory (dev) and Redis (production, via fakeredis)."""

from __future__ import annotations

from types import SimpleNamespace

import fakeredis

from app.services.rate_limit import (
    InMemoryRateLimitBackend,
    RedisRateLimitBackend,
    get_rate_limit_backend,
)


def test_in_memory_backend_enforces_limit():
    backend = InMemoryRateLimitBackend()
    assert backend.allow("1.2.3.4", limit=3, window_seconds=3600) is True
    assert backend.allow("1.2.3.4", limit=3, window_seconds=3600) is True
    assert backend.allow("1.2.3.4", limit=3, window_seconds=3600) is True
    assert backend.allow("1.2.3.4", limit=3, window_seconds=3600) is False
    # A different key is independent.
    assert backend.allow("5.6.7.8", limit=3, window_seconds=3600) is True


def test_redis_backend_enforces_limit_and_sets_expiry():
    client = fakeredis.FakeStrictRedis()
    backend = RedisRateLimitBackend(client)
    for _ in range(3):
        assert backend.allow("ip", limit=3, window_seconds=3600) is True
    assert backend.allow("ip", limit=3, window_seconds=3600) is False
    # A TTL was set on first hit.
    assert client.ttl("ratelimit:ip") > 0


def test_redis_backend_is_shared_across_instances():
    # Two backend instances over the same store share the counter — the
    # property the in-process backend lacks.
    client = fakeredis.FakeStrictRedis()
    a = RedisRateLimitBackend(client)
    b = RedisRateLimitBackend(client)
    assert a.allow("ip", limit=2, window_seconds=60) is True
    assert b.allow("ip", limit=2, window_seconds=60) is True
    assert a.allow("ip", limit=2, window_seconds=60) is False


def test_backend_selection_defaults_to_memory():
    memory_settings = SimpleNamespace(rate_limit_backend="memory", redis_url=None)
    assert isinstance(get_rate_limit_backend(memory_settings), InMemoryRateLimitBackend)


def test_backend_selection_builds_redis_when_configured():
    # from_url builds a client lazily (no connection made here), so this is safe
    # without a running Redis.
    redis_settings = SimpleNamespace(
        rate_limit_backend="redis", redis_url="redis://localhost:6379/0"
    )
    backend = get_rate_limit_backend(redis_settings)
    assert isinstance(backend, RedisRateLimitBackend)
    # Re-selecting memory heals the cached backend (no cross-request leak).
    memory_settings = SimpleNamespace(rate_limit_backend="memory", redis_url=None)
    assert isinstance(get_rate_limit_backend(memory_settings), InMemoryRateLimitBackend)
