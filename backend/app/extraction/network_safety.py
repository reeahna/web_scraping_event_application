"""Connect-time network safety: DNS resolution + resolved-IP validation.

`app.core.url_safety` validates URLs at *string* level (scheme, embedded
credentials, hostname suffix, and — only when the hostname is already a
literal IP — the IP itself). Its own docstring explains why it stops there:
resolving domain names is a network operation with its own risk, deferred to
"once the real extraction engine exists."  This module is that connect-time
complement.

Before every HTTP request the extraction engine makes — the initial request
*and* every redirect hop — it resolves the hostname and validates every
returned address, failing closed if any of them is unsafe. This closes the
gap where a domain name is DNS-rebound to a private address after passing
the string check at configuration-save time.

Residual limitation (documented, not hidden): this is "validate immediately
before connecting," not "pin the TCP connection to the exact validated IP."
There is a narrow TOCTOU window between this resolution and httpx's own
subsequent connection, milliseconds later in the same call. Fully closing
that window would require rewriting each request to connect directly to the
validated IP via a custom transport while preserving TLS SNI/hostname
verification — materially more complex, harder to test without a live TLS
server, and risks silently weakening certificate validation if implemented
incorrectly. Accepted tradeoff for this admin-triggered (not continuously
polling) engine.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from app.core.url_safety import UnsafeURLError, validate_public_url

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# Documentation-only ranges the stdlib `ipaddress` module does NOT flag via
# is_private/is_reserved, but which must never be a real extraction target.
_DOCUMENTATION_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("192.0.2.0/24"),  # TEST-NET-1
    ipaddress.ip_network("198.51.100.0/24"),  # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),  # TEST-NET-3
    ipaddress.ip_network("2001:db8::/32"),  # IPv6 documentation range
)


class BlockedHostError(UnsafeURLError):
    """Raised when a string check or connect-time resolution finds an unsafe
    destination. Callers should map this to a `blocked` FetchResponse, never
    a `failed` one — this is a safety block, not a target-site error."""


def is_ip_blocked(ip: IpAddress) -> bool:
    """Extends app.core.url_safety's `_ip_is_blocked` with: IPv4-mapped IPv6
    unwrapping (::ffff:127.0.0.1) and documentation-only ranges. Duplicated
    rather than imported from url_safety because that helper is private and
    scoped to string-literal validation; this one is the connect-time,
    resolved-address surface with a slightly larger blocklist."""
    if isinstance(ip, ipaddress.IPv6Address):
        mapped = ip.ipv4_mapped
        if mapped is not None:
            return is_ip_blocked(mapped)
    if any(ip in network for network in _DOCUMENTATION_NETWORKS):
        return True
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


async def resolve_and_validate_host(hostname: str, port: int) -> list[IpAddress]:
    """Resolve `hostname` via the running event loop's own non-blocking
    resolver (never a bare, thread-blocking `socket.getaddrinfo`) and
    validate every returned A/AAAA address. Returns the validated address
    list on success; raises BlockedHostError if the host doesn't resolve or
    if ANY resolved address is unsafe.

    Fails closed on a mixed public+private answer set rather than picking
    the "safe" one — when this matters at all, the answer is fully
    attacker-controlled, so treating a mixed set as suspicious and rejecting
    outright is the simpler, more conservative choice."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedHostError(f"Could not resolve host '{hostname}': {exc}") from exc

    if not infos:
        raise BlockedHostError(f"Host '{hostname}' did not resolve to any address")

    addresses: list[IpAddress] = []
    for info in infos:
        raw_ip = info[4][0]
        # IPv6 addresses from getaddrinfo may carry a scope id (e.g. "%eth0").
        raw_ip = raw_ip.split("%", 1)[0]
        addresses.append(ipaddress.ip_address(raw_ip))

    unsafe = [ip for ip in addresses if is_ip_blocked(ip)]
    if unsafe:
        raise BlockedHostError(
            f"Host '{hostname}' resolves to a blocked address range ({unsafe[0]})"
        )
    return addresses


def validate_request_url(url: str) -> str:
    """String-level pre-check (scheme, credentials, host suffix, and literal-
    IP validation) — the same rules used at configuration-save time,
    re-applied at fetch time since this is a security boundary, not just a
    form validator. Callers must still call resolve_and_validate_host()
    before connecting; this does not resolve domain names."""
    try:
        return validate_public_url(url)
    except UnsafeURLError as exc:
        raise BlockedHostError(str(exc)) from exc


def hostname_and_port(url: str) -> tuple[str, int]:
    parsed = urlsplit(url)
    hostname = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return hostname, port
