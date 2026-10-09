"""Fills in a missing event description from the event's own page.

Many listings (calendar grids, card pages, feeds) carry only a title, date and
link, so the event's description lives on its detail page. Unlike
`detail_pages.py`, which applies selectors an administrator (or onboarding)
configured for one site, this is site-agnostic: it reads the two places almost
every event page publishes a summary of itself —

1. a schema.org Event in JSON-LD (its `description`), then
2. the page's `og:description` / `twitter:description` / `<meta name="description">`.

Bounded and conservative:

* only candidates with no description (or summary/excerpt) of their own are
  looked up, and URLs the caller already has a description for are skipped, so
  repeated runs spend the budget on new events;
* at most `max_fetches` distinct pages per run, through the same FetchStrategy
  (SSRF/redirect/byte protections) as the listing;
* a meta description shared by several different pages, or identical to the
  listing page's own, is site boilerplate ("The official calendar of ...") and
  is dropped rather than shown as every event's description.

A failed or empty lookup leaves the candidate untouched apart from a warning;
it never blocks the event.
"""

from __future__ import annotations

import dataclasses
import json
from collections import Counter
from typing import Any
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from app.extraction.fetch import FetchStrategy
from app.extraction.normalize import DESCRIPTION_FIELDS
from app.extraction.sanitize import strip_to_text
from app.extraction.types import EventCandidate, FetchRequest
from app.schemas.extraction import FetchConfig

# Default per-run page budget for this lookup.
DEFAULT_MAX_FETCHES = 15

# Shorter than this is a label ("Free", "Details"), not a description.
_MIN_LENGTH = 20

_META_KEYS = (
    ("property", "og:description"),
    ("name", "twitter:description"),
    ("name", "description"),
)

_SOURCE_JSONLD = "detail:jsonld.description"


def has_description(raw: dict[str, Any]) -> bool:
    for field in DESCRIPTION_FIELDS:
        value = raw.get(field)
        if isinstance(value, str) and strip_to_text(value):
            return True
    return False


def _flatten(node: Any) -> list[dict[str, Any]]:
    if isinstance(node, list):
        return [item for child in node for item in _flatten(child)]
    if isinstance(node, dict):
        if isinstance(node.get("@graph"), list):
            return _flatten(node["@graph"])
        return [node]
    return []


def _is_event(node: dict[str, Any]) -> bool:
    types = node.get("@type")
    if isinstance(types, str):
        types = [types]
    return isinstance(types, list) and any("event" in str(t).lower() for t in types)


def _same_page(a: str, b: str) -> bool:
    pa, pb = urlsplit(a), urlsplit(b)
    return (pa.netloc.lower(), pa.path.rstrip("/")) == (pb.netloc.lower(), pb.path.rstrip("/"))


def _clean(value: Any, title: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    text = strip_to_text(value)
    if not text or len(text) < _MIN_LENGTH:
        return None
    if title and text.casefold() == title.strip().casefold():
        return None
    return text


def _jsonld_description(soup: BeautifulSoup, page_url: str, title: str | None) -> str | None:
    """The page's Event description. A page listing several events is only
    trusted for the one whose `url` is this page; otherwise it must hold
    exactly one Event."""
    events: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        text = script.string or script.get_text()
        if not text or not text.strip():
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        events.extend(node for node in _flatten(data) if _is_event(node))
    for node in events:
        url = node.get("url")
        if isinstance(url, str) and _same_page(urljoin(page_url, url), page_url):
            return _clean(node.get("description"), title)
    if len(events) == 1:
        return _clean(events[0].get("description"), title)
    return None


def meta_description(soup: BeautifulSoup, title: str | None = None) -> str | None:
    for attr, key in _META_KEYS:
        tag = soup.find("meta", attrs={attr: key})
        if tag is not None:
            value = _clean(tag.get("content"), title)
            if value:
                return value
    return None


def description_from_page(
    html: str, page_url: str, title: str | None = None
) -> tuple[str, str] | None:
    """(description, source path) read from an event page, or None."""
    soup = BeautifulSoup(html, "html.parser")
    jsonld = _jsonld_description(soup, page_url, title)
    if jsonld:
        return jsonld, _SOURCE_JSONLD
    meta = meta_description(soup, title)
    if meta:
        return meta, "detail:meta.description"
    return None


def _lookup_url(candidate: EventCandidate) -> tuple[str | None, str | None]:
    """(page to fetch, canonical URL as it will be stored)."""
    raw = candidate.raw
    canonical = raw.get("canonical_url")
    canonical_url = urljoin(candidate.source_page, str(canonical)) if canonical else None
    detail = raw.get("detail_link")
    page = urljoin(candidate.source_page, str(detail)) if detail else canonical_url
    if not page or urlsplit(page).scheme not in ("http", "https"):
        return None, canonical_url
    return page, canonical_url


async def fill_missing_descriptions(
    candidates: list[EventCandidate],
    fetch: FetchStrategy,
    fetch_config: FetchConfig,
    *,
    listing_html: str | None = None,
    known_described_urls: frozenset[str] = frozenset(),
    max_fetches: int = DEFAULT_MAX_FETCHES,
) -> tuple[list[EventCandidate], list[str]]:
    """Returns the candidates (those that gained a description replaced, the
    rest unchanged, order kept) and run-level warnings."""
    site_boilerplate: str | None = None
    if listing_html:
        site_boilerplate = meta_description(BeautifulSoup(listing_html, "html.parser"))

    found: dict[str, tuple[str, str] | None] = {}
    skipped = 0
    for candidate in candidates:
        if max_fetches <= 0:
            break
        if has_description(candidate.raw):
            continue
        page, canonical_url = _lookup_url(candidate)
        if page is None or page in found:
            continue
        if canonical_url in known_described_urls or page in known_described_urls:
            continue
        if len(found) >= max_fetches:
            skipped += 1
            continue
        response = await fetch.fetch(FetchRequest(url=page), fetch_config)
        if response.blocked_reason is not None or response.status_code != 200:
            found[page] = None
            continue
        title = candidate.raw.get("title")
        found[page] = description_from_page(
            response.text, page, title if isinstance(title, str) else None
        )

    # A meta description repeated across different pages describes the site,
    # not the event. JSON-LD Event descriptions are per-event by construction
    # (a recurring event's pages may legitimately share one) and are kept.
    meta_counts = Counter(
        value for value, source in filter(None, found.values()) if source != _SOURCE_JSONLD
    )

    filled = 0
    result: list[EventCandidate] = []
    for candidate in candidates:
        page, _ = (None, None) if has_description(candidate.raw) else _lookup_url(candidate)
        hit = found.get(page) if page else None
        if hit is not None:
            value, source = hit
            boilerplate = source != _SOURCE_JSONLD and (
                meta_counts[value] > 1 or value == site_boilerplate
            )
            if not boilerplate:
                raw = {**candidate.raw, "description": value}
                paths = {**candidate.field_source_paths, "description": source}
                candidate = dataclasses.replace(candidate, raw=raw, field_source_paths=paths)
                filled += 1
        result.append(candidate)

    warnings: list[str] = []
    if found:
        warnings.append(f"description_lookup:{filled}_filled_of_{len(found)}_pages")
    if skipped:
        warnings.append(f"description_lookup_budget_reached:{skipped}_events_not_looked_up")
    return result, warnings
