"""Reusable SSRF-safe URL validation for admin-submitted website URLs.

Scope note: this is string/hostname-level validation only — it does NOT
perform DNS resolution. Resolving attacker-controlled hostnames is itself a
network operation (and doesn't fully close the SSRF hole anyway, due to DNS
rebinding between validation-time and fetch-time). Once the real extraction
engine exists and actually connects to these URLs, it must re-validate the
resolved IP address at connect-time — this validator only catches what's
knowable from the URL string itself.
"""

import ipaddress
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})

_BLOCKED_HOSTS = frozenset({"localhost", "metadata.google.internal"})
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal")


class UnsafeURLError(ValueError):
    """Raised when a submitted URL fails SSRF-safety validation."""


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _parse_numeric_ipv4(hostname: str) -> ipaddress.IPv4Address | None:
    """Catches the classic SSRF bypass of encoding an IPv4 address as a single
    decimal integer (e.g. 2130706433 == 127.0.0.1)."""
    if hostname.isdigit():
        try:
            return ipaddress.IPv4Address(int(hostname))
        except (ValueError, ipaddress.AddressValueError):
            return None
    return None


def validate_public_url(url: str) -> str:
    """Validate that `url` is safe to store as a scrape target. Returns the
    (stripped) URL on success; raises UnsafeURLError with a clear reason otherwise."""
    url = (url or "").strip()
    if not url:
        raise UnsafeURLError("URL is required")

    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise UnsafeURLError("Malformed URL") from exc

    scheme = parsed.scheme.lower()
    if not scheme:
        raise UnsafeURLError("URL must include an http:// or https:// scheme")
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f"Unsupported URL scheme '{scheme}' — only http/https are allowed")

    if parsed.username or parsed.password:
        raise UnsafeURLError("URLs with embedded credentials are not allowed")

    try:
        hostname = (parsed.hostname or "").lower()
    except ValueError as exc:
        # e.g. an invalid IPv6 literal in brackets
        raise UnsafeURLError("Malformed URL") from exc

    if not hostname:
        raise UnsafeURLError("Malformed URL: missing host")

    if hostname in _BLOCKED_HOSTS or any(
        hostname.endswith(suffix) for suffix in _BLOCKED_HOST_SUFFIXES
    ):
        raise UnsafeURLError(f"'{hostname}' is not an allowed host")

    ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        ip = _parse_numeric_ipv4(hostname)

    if ip is not None and _ip_is_blocked(ip):
        raise UnsafeURLError(f"'{hostname}' resolves to a blocked address range ({ip})")

    return url
