import ipaddress
import socket
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.extraction.fetch import HttpFetchStrategy
from app.extraction.network_safety import BlockedHostError, is_ip_blocked, resolve_and_validate_host
from app.extraction.types import FetchRequest
from app.schemas.extraction import FetchConfig

# --- is_ip_blocked: every required address family/category -----------------


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "10.0.0.5",  # private
        "192.168.1.1",  # private
        "169.254.169.254",  # link-local (cloud metadata)
        "0.0.0.0",  # unspecified
        "224.0.0.1",  # multicast
        "192.0.2.1",  # documentation (TEST-NET-1)
        "198.51.100.1",  # documentation (TEST-NET-2)
        "203.0.113.1",  # documentation (TEST-NET-3)
        "::1",  # IPv6 loopback
        "fd00::1",  # IPv6 private (unique local)
        "fe80::1",  # IPv6 link-local
        "2001:db8::1",  # IPv6 documentation
        "::ffff:127.0.0.1",  # IPv4-mapped IPv6 loopback
        "::ffff:10.0.0.1",  # IPv4-mapped IPv6 private
    ],
)
def test_is_ip_blocked_covers_every_required_range(ip):
    assert is_ip_blocked(ipaddress.ip_address(ip)) is True


@pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"])
def test_is_ip_blocked_allows_public_addresses(ip):
    assert is_ip_blocked(ipaddress.ip_address(ip)) is False


# --- String-level checks (no DNS/network involved) --------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://10.0.0.5/",
        "http://[fd00::1]/",
        "http://169.254.169.254/",
        "http://2130706433/",  # decimal notation for 127.0.0.1
        "http://user:pass@example.com/",
    ],
)
@pytest.mark.asyncio
async def test_fetch_blocks_unsafe_urls_without_any_network_call(url):
    strategy = HttpFetchStrategy()
    response = await strategy.fetch(FetchRequest(url=url), FetchConfig())
    assert response.blocked_reason is not None
    assert response.status_code == 0


# --- Connect-time DNS resolution (mocked resolver, no live DNS) -------------


def _fake_getaddrinfo(*addresses: str):
    async def _resolve(host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port)) for addr in addresses]

    return _resolve


@pytest.mark.asyncio
async def test_hostname_resolving_to_private_ip_is_blocked(monkeypatch):
    class FakeLoop:
        getaddrinfo = staticmethod(_fake_getaddrinfo("10.0.0.5"))

    monkeypatch.setattr(
        "app.extraction.network_safety.asyncio.get_running_loop", lambda: FakeLoop()
    )
    with pytest.raises(BlockedHostError):
        await resolve_and_validate_host("evil.example.com", 443)


@pytest.mark.asyncio
async def test_mixed_public_and_private_dns_answers_fail_closed(monkeypatch):
    class FakeLoop:
        getaddrinfo = staticmethod(_fake_getaddrinfo("93.184.216.34", "127.0.0.1"))

    monkeypatch.setattr(
        "app.extraction.network_safety.asyncio.get_running_loop", lambda: FakeLoop()
    )
    with pytest.raises(BlockedHostError):
        await resolve_and_validate_host("mixed.example.com", 443)


@pytest.mark.asyncio
async def test_dns_rebinding_simulation_each_call_independently_validated(monkeypatch):
    """Simulates DNS rebinding: the first lookup returns a safe address, the
    second (as if the attacker's DNS server flipped the answer) returns a
    private one. Demonstrates the engine never caches or trusts a prior
    resolution — every call is independently validated."""
    calls = {"count": 0}

    async def _resolve(host, port, **kwargs):
        calls["count"] += 1
        addr = "93.184.216.34" if calls["count"] == 1 else "127.0.0.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, port))]

    class FakeLoop:
        getaddrinfo = staticmethod(_resolve)

    monkeypatch.setattr(
        "app.extraction.network_safety.asyncio.get_running_loop", lambda: FakeLoop()
    )
    first_result = await resolve_and_validate_host("rebind.example.com", 443)
    assert not is_ip_blocked(first_result[0])
    with pytest.raises(BlockedHostError):
        await resolve_and_validate_host("rebind.example.com", 443)


@pytest.mark.asyncio
async def test_unresolvable_hostname_is_blocked_not_a_crash(monkeypatch):
    async def _raise(*args, **kwargs):
        raise socket.gaierror("name or service not known")

    class FakeLoop:
        getaddrinfo = staticmethod(_raise)

    monkeypatch.setattr(
        "app.extraction.network_safety.asyncio.get_running_loop", lambda: FakeLoop()
    )
    with pytest.raises(BlockedHostError):
        await resolve_and_validate_host("nonexistent.invalid", 443)


# --- Redirect handling (mocked transport + mocked DNS check) ----------------


def _patched_dns():
    return patch(
        "app.extraction.fetch.resolve_and_validate_host",
        new=AsyncMock(return_value=[ipaddress.ip_address("93.184.216.34")]),
    )


@pytest.mark.asyncio
async def test_redirect_to_private_ip_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    with _patched_dns():
        strategy = HttpFetchStrategy(transport=httpx.MockTransport(handler))
        response = await strategy.fetch(
            FetchRequest(url="https://example.com/events"), FetchConfig()
        )
    assert response.blocked_reason is not None
    assert "127.0.0.1" in response.blocked_reason


@pytest.mark.asyncio
async def test_maximum_redirect_count_enforced():
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(
            302, headers={"location": f"https://example.com/page{call_count['n']}"}
        )

    with _patched_dns():
        strategy = HttpFetchStrategy(transport=httpx.MockTransport(handler))
        response = await strategy.fetch(
            FetchRequest(url="https://example.com/events"),
            FetchConfig(max_redirects=3),
        )
    assert response.blocked_reason == "too_many_redirects"
    assert call_count["n"] == 4  # initial + 3 redirect hops, then give up


@pytest.mark.asyncio
async def test_successful_redirect_chain_followed_and_recorded():
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://example.com/events":
            return httpx.Response(302, headers={"location": "https://example.com/events/final"})
        return httpx.Response(200, text="ok", headers={"content-type": "text/html"})

    with _patched_dns():
        strategy = HttpFetchStrategy(transport=httpx.MockTransport(handler))
        response = await strategy.fetch(
            FetchRequest(url="https://example.com/events"), FetchConfig()
        )
    assert response.status_code == 200
    assert response.final_url == "https://example.com/events/final"
    assert response.redirect_history == ("https://example.com/events",)


# --- Oversized response / content-type / timeout / retry / blocked --------


@pytest.mark.asyncio
async def test_oversized_response_aborted_safely():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 10_000, headers={"content-type": "text/html"})

    with _patched_dns():
        strategy = HttpFetchStrategy(transport=httpx.MockTransport(handler))
        response = await strategy.fetch(
            FetchRequest(url="https://example.com/events"),
            FetchConfig(max_response_bytes=100),
        )
    assert response.truncated is True
    assert len(response.body) <= 100


@pytest.mark.asyncio
async def test_unsupported_content_type_surfaced_on_response():
    from app.extraction.fetch import content_type_allowed

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="binary-ish", headers={"content-type": "application/pdf"})

    with _patched_dns():
        strategy = HttpFetchStrategy(transport=httpx.MockTransport(handler))
        response = await strategy.fetch(
            FetchRequest(url="https://example.com/events"), FetchConfig()
        )
    assert not content_type_allowed(response.content_type, FetchConfig())


@pytest.mark.asyncio
async def test_timeout_reported_as_failed_not_a_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    with _patched_dns():
        strategy = HttpFetchStrategy(transport=httpx.MockTransport(handler))
        response = await strategy.fetch(
            FetchRequest(url="https://example.com/events"),
            FetchConfig(max_retries=0),
        )
    assert response.blocked_reason == "failed:timeout"


@pytest.mark.asyncio
async def test_retryable_5xx_is_retried_then_succeeds():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, text="ok", headers={"content-type": "text/html"})

    with _patched_dns():
        strategy = HttpFetchStrategy(transport=httpx.MockTransport(handler))
        response = await strategy.fetch(
            FetchRequest(url="https://example.com/events"),
            FetchConfig(max_retries=2, retry_backoff_seconds=0),
        )
    assert response.status_code == 200
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_client_error_not_retried():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(404)

    with _patched_dns():
        strategy = HttpFetchStrategy(transport=httpx.MockTransport(handler))
        response = await strategy.fetch(
            FetchRequest(url="https://example.com/events"),
            FetchConfig(max_retries=2, retry_backoff_seconds=0),
        )
    assert response.status_code == 404
    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_blocked_or_challenge_response_detected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Access Denied", headers={"content-type": "text/html"})

    with _patched_dns():
        strategy = HttpFetchStrategy(transport=httpx.MockTransport(handler))
        response = await strategy.fetch(
            FetchRequest(url="https://example.com/events"), FetchConfig()
        )
    assert response.blocked_reason is not None


@pytest.mark.asyncio
async def test_detail_page_fetch_gets_same_ssrf_protection():
    """A detail-page fetch (used by generic_html_cards enrichment) reuses
    HttpFetchStrategy.fetch() directly — this just confirms an unsafe detail
    link is blocked exactly like a listing-page fetch would be."""
    strategy = HttpFetchStrategy()
    response = await strategy.fetch(
        FetchRequest(url="http://127.0.0.1/event-detail"), FetchConfig()
    )
    assert response.blocked_reason is not None
