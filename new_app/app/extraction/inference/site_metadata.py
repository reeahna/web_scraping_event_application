"""Deterministic site-metadata inference.

Answers the question bulk onboarding would otherwise have to ask an
administrator for every single URL: what is this site called, and what is its
base URL? Pure — takes one already-fetched document plus its final URL, does
no I/O, and (like everything else in this package) never branches on a
hostname: the hostname is only ever used as the *last* fallback value, never
as a condition selecting different logic.

Name evidence, in descending order of reliability:

1. ``<meta property="og:site_name">`` — the site stating its own name
2. a schema.org ``Organization`` / ``WebSite`` node's ``name``
3. ``<title>`` with a trailing site-section suffix removed
4. the hostname, de-``www``-ed and title-cased

Every value is recorded alongside which rule produced it, so the admin UI can
show what was inferred rather than presenting a guess as a fact.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from bs4 import BeautifulSoup

# Splits "Events | Riverside Arts Center" or "Events - Riverside Arts Center"
# into its parts. Only used to *drop* a segment, never to invent one.
_TITLE_SEPARATOR_RE = re.compile(r"\s+[|–—·]\s+|\s+-\s+")

# Leading segments that describe the page rather than the site. Matched
# case-insensitively against a whole segment, so a venue genuinely called
# "The Calendar" is not truncated to nothing.
_PAGE_SEGMENTS: frozenset[str] = frozenset(
    {
        "events",
        "event",
        "calendar",
        "calendars",
        "events calendar",
        "event calendar",
        "upcoming events",
        "all events",
        "whats on",
        "what's on",
        "shows",
        "tickets",
        "home",
        "schedule",
    }
)

_ORGANIZATION_TYPES: frozenset[str] = frozenset(
    {"organization", "website", "performingartstheater", "localbusiness", "place"}
)

_MAX_NAME_LENGTH = 255


def _clean(value: str | None) -> str | None:
    if not value:
        return None
    text = " ".join(str(value).split())
    return text[:_MAX_NAME_LENGTH] or None


def normalize_origin(url: str) -> str:
    """The site's base URL: scheme + host (+ explicit port), nothing else."""
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold()
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme.casefold(), f"{host}{port}", "", "", ""))


def hostname_label(url: str) -> str | None:
    """"https://www.riverside-arts.example.org/events" -> "Riverside Arts"."""
    host = (urlsplit(url).hostname or "").casefold()
    if not host:
        return None
    host = host.removeprefix("www.")
    # Drop the public suffix-ish tail; keeping only the first label is wrong
    # for "arts.example.org", so keep everything before the final two labels
    # when there are more than two, otherwise the first label.
    labels = host.split(".")
    stem = labels[0] if len(labels) <= 2 else ".".join(labels[:-2]) or labels[0]
    return _clean(stem.replace("-", " ").replace("_", " ").title())


def _og_site_name(soup: BeautifulSoup) -> str | None:
    tag = soup.find("meta", attrs={"property": "og:site_name"})
    if tag is None:
        tag = soup.find("meta", attrs={"name": "og:site_name"})
    return _clean(tag.get("content")) if tag else None


def _structured_org_name(soup: BeautifulSoup) -> str | None:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        text = script.string or script.get_text()
        if not text or not text.strip():
            continue
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            continue
        for node in _flatten(data):
            type_value = node.get("@type")
            types = type_value if isinstance(type_value, list) else [type_value]
            if any(isinstance(t, str) and t.lower() in _ORGANIZATION_TYPES for t in types):
                name = _clean(node.get("name"))
                if name:
                    return name
    return None


def _flatten(node: object) -> list[dict]:
    if isinstance(node, list):
        out: list[dict] = []
        for item in node:
            out.extend(_flatten(item))
        return out
    if isinstance(node, dict):
        if isinstance(node.get("@graph"), list):
            return _flatten(node["@graph"])
        return [node]
    return []


def _title_name(soup: BeautifulSoup) -> str | None:
    title = soup.find("title")
    if title is None:
        return None
    text = _clean(title.get_text())
    if not text:
        return None
    segments = [s.strip() for s in _TITLE_SEPARATOR_RE.split(text) if s.strip()]
    if len(segments) > 1:
        kept = [s for s in segments if s.casefold() not in _PAGE_SEGMENTS]
        if kept:
            # The site name conventionally sits last ("Events | Site Name");
            # when every segment survives, prefer the longest, which is the
            # one least likely to be a section label.
            return _clean(kept[-1] if len(kept) < len(segments) else max(kept, key=len))
    return text


@dataclass(frozen=True)
class SiteMetadata:
    name: str
    source_display_name: str
    base_url: str
    event_listing_url: str
    # Which of the above were inferred rather than supplied, and by which
    # rule — surfaced in the UI so nothing reads as authoritative when it
    # isn't.
    inferred_fields: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "source_display_name": self.source_display_name,
            "base_url": self.base_url,
            "event_listing_url": self.event_listing_url,
            "inferred_fields": dict(self.inferred_fields),
        }


def infer_site_metadata(
    *,
    document: str | None,
    final_url: str,
    submitted_url: str,
    supplied_name: str | None = None,
    supplied_source_display_name: str | None = None,
) -> SiteMetadata:
    """`document` may be None (the page could not be fetched) — inference then
    falls straight through to the hostname, which is always available from the
    URL itself. A supplied name is never overridden."""
    inferred: dict[str, str] = {}

    name = _clean(supplied_name)
    if name is None and document:
        soup = BeautifulSoup(document, "html.parser")
        for rule, value in (
            ("og:site_name", _og_site_name(soup)),
            ("schema.org Organization/WebSite name", _structured_org_name(soup)),
            ("<title> with page-section suffix removed", _title_name(soup)),
        ):
            if value:
                name, inferred["name"] = value, rule
                break
    if name is None:
        name = hostname_label(final_url) or urlsplit(final_url).hostname or submitted_url
        inferred["name"] = "hostname fallback"

    display = _clean(supplied_source_display_name)
    if display is None:
        display = name
        inferred["source_display_name"] = (
            "inferred site name" if "name" in inferred else "supplied site name"
        )

    base_url = normalize_origin(final_url)
    inferred["base_url"] = "normalized origin of the final URL"
    # The submitted URL is what the administrator pointed at, so it stays the
    # listing URL. A proposer that discovers a better endpoint records it in
    # the site configuration, not here.
    return SiteMetadata(
        name=name,
        source_display_name=display,
        base_url=base_url,
        event_listing_url=submitted_url,
        inferred_fields=inferred,
    )
