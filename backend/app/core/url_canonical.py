"""One canonical form for comparing website URLs.

Every "is this the same source?" decision — bulk onboarding's duplicate
checks, redirect-target matching, and the duplicate guard on manual website
creation — compares `canonical_url()` output on both sides. Submitted and
stored URLs go through the *same* function, so a difference in trailing
slash, host casing, default port, or fragment can never produce two rows for
one source.

Why this is not `app.services.fingerprints.normalize_url`: that function's
output is baked into persisted `Event.fingerprint` values. Changing its
semantics would silently change the identity of already-stored events. This
module is therefore a separate, stricter canonicalizer used only for website
matching, and the two are deliberately allowed to differ.

Normalization applied:

* scheme and host lowercased
* IDNA/punycode host normalization (unicode host -> ascii), when encodable
* default ports removed (80 for http, 443 for https)
* empty path treated as "/"
* trailing slash removed from non-root paths
* fragment removed
* query keys sorted, blank values preserved, empty query removed

Deliberately NOT applied: nothing that equates different paths. `/events`
and `/calendar` are different resources and must stay different.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}


def _canonical_host(hostname: str | None) -> str:
    host = (hostname or "").strip().casefold()
    if not host:
        return ""
    if host.isascii():
        return host
    try:
        return host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        # A host that can't be IDNA-encoded is compared as-is rather than
        # dropped — being unable to canonicalize must never silently make
        # two different hosts look equal.
        return host


def canonical_url(url: str | None) -> str:
    """The comparison form of `url`. Returns "" for an empty/unparseable
    value, and "" never matches "" (see `same_resource`)."""
    value = (url or "").strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""

    scheme = parsed.scheme.casefold()
    try:
        host = _canonical_host(parsed.hostname)
    except ValueError:
        return ""
    if not host:
        return ""

    port = ""
    try:
        if parsed.port is not None and parsed.port != _DEFAULT_PORTS.get(scheme):
            port = f":{parsed.port}"
    except ValueError:
        port = ""

    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"

    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit((scheme, f"{host}{port}", path, query, ""))


def canonical_origin(url: str | None) -> str:
    """Scheme + host (+ non-default port), with the path reduced to "/"."""
    canonical = canonical_url(url)
    if not canonical:
        return ""
    parsed = urlsplit(canonical)
    return urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))


def is_origin_only(url: str | None) -> bool:
    """True when the URL addresses a site rather than a page within it."""
    canonical = canonical_url(url)
    return bool(canonical) and urlsplit(canonical).path == "/"


def same_resource(left: str | None, right: str | None) -> bool:
    """Equality on canonical form. Two unparseable values are never equal —
    "I couldn't understand either of these" is not evidence of a match."""
    left_canonical = canonical_url(left)
    right_canonical = canonical_url(right)
    if not left_canonical or not right_canonical:
        return False
    return left_canonical == right_canonical
