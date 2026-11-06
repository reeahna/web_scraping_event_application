"""Fills in an event's missing description and venue from the event's own page.

Many listings (calendar grids, card pages, feeds) carry only a title, date and
link, so the event's description and venue live on its detail page. Unlike
`detail_pages.py`, which applies selectors an administrator (or onboarding)
configured for one site, this is site-agnostic: it reads the places almost
every event page publishes about itself.

Description:
1. a schema.org Event in JSON-LD (its `description`), then
2. the page's `og:description` / `twitter:description` / `<meta name="description">`.

Venue (and address, when the event has none):
1. the JSON-LD Event's `location` (a Place, a plain string, or a list), then
2. schema.org microdata: the page's single `itemprop="location"` element.

Bounded and conservative:

* only candidates missing a description (or summary/excerpt) or a venue (or
  carrying a placeholder like "TBA") are looked up, and URLs the caller already
  has that field for are skipped, so repeated runs spend the budget on events
  that still need it;
* at most `max_fetches` distinct pages per run, through the same FetchStrategy
  (SSRF/redirect/byte protections) as the listing; one fetch serves both fields;
* a meta description shared by several different pages, or identical to the
  listing page's own, is site boilerplate ("The official calendar of ...") and
  is dropped rather than shown as every event's description;
* only what the source page itself states is used: no venue is guessed from
  free text.

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
from app.extraction.locations import clean_venue, has_venue, venue_from_location
from app.extraction.normalize import DESCRIPTION_FIELDS
from app.extraction.sanitize import strip_to_text
from app.extraction.types import EventCandidate, FetchRequest
from app.schemas.extraction import FetchConfig

# Default per-run page budget for this lookup.
DEFAULT_MAX_FETCHES = 30

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


def _page_event(soup: BeautifulSoup, page_url: str) -> dict[str, Any] | None:
    """The page's own JSON-LD Event. A page listing several events is only
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
            return node
    if len(events) == 1:
        return events[0]
    return None


def meta_description(soup: BeautifulSoup, title: str | None = None) -> str | None:
    for attr, key in _META_KEYS:
        tag = soup.find("meta", attrs={attr: key})
        if tag is not None:
            value = _clean(tag.get("content"), title)
            if value:
                return value
    return None


def _description(
    soup: BeautifulSoup, event: dict[str, Any] | None, title: str | None
) -> tuple[str, str] | None:
    jsonld = _clean(event.get("description"), title) if event else None
    if jsonld:
        return jsonld, _SOURCE_JSONLD
    meta = meta_description(soup, title)
    if meta:
        return meta, "detail:meta.description"
    return None


def description_from_page(
    html: str, page_url: str, title: str | None = None
) -> tuple[str, str] | None:
    """(description, source path) read from an event page, or None."""
    soup = BeautifulSoup(html, "html.parser")
    return _description(soup, _page_event(soup, page_url), title)


@dataclasses.dataclass(frozen=True)
class PageVenue:
    venue: str | None
    address: str | None
    source: str


def _microdata_venue(soup: BeautifulSoup, title: str | None) -> PageVenue | None:
    # Only a page with exactly one location is trusted: a page that marks up
    # several (related events, a sidebar) can't say which is this event's.
    locations = soup.find_all(attrs={"itemprop": "location"})
    if len(locations) != 1:
        return None
    location = locations[0]
    name_tag = location.find(attrs={"itemprop": "name"})
    address_tag = location.find(attrs={"itemprop": "address"})
    if name_tag is not None:
        venue = clean_venue(name_tag.get("content") or name_tag.get_text(" "), title)
    elif address_tag is None:
        venue = clean_venue(location.get("content") or location.get_text(" "), title)
    else:
        venue = None
    address = None
    if address_tag is not None:
        address = clean_venue(address_tag.get("content") or address_tag.get_text(" "))
    if venue is None and address is None:
        return None
    return PageVenue(venue, address, "detail:microdata.location")


def _venue(
    soup: BeautifulSoup, event: dict[str, Any] | None, title: str | None
) -> PageVenue | None:
    if event is not None:
        venue, address = venue_from_location(event.get("location"))
        if venue is not None and title and venue.casefold() == title.strip().casefold():
            venue = None
        if venue is not None or address is not None:
            return PageVenue(venue, address, "detail:jsonld.location")
    return _microdata_venue(soup, title)


def venue_from_page(html: str, page_url: str, title: str | None = None) -> PageVenue | None:
    """The venue (and address) an event page states for itself, or None."""
    soup = BeautifulSoup(html, "html.parser")
    return _venue(soup, _page_event(soup, page_url), title)


@dataclasses.dataclass(frozen=True)
class _PageFindings:
    description: tuple[str, str] | None
    venue: PageVenue | None


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




def _needs(
    candidate: EventCandidate,
    page: str,
    canonical_url: str | None,
    known_described_urls: frozenset[str],
    known_venue_urls: frozenset[str],
) -> tuple[bool, bool]:
    """(needs a description, needs a venue) for this candidate's page."""

    def known(urls: frozenset[str]) -> bool:
        return canonical_url in urls or page in urls

    wants_description = not has_description(candidate.raw) and not known(known_described_urls)
    wants_venue = not has_venue(candidate.raw) and not known(known_venue_urls)
    return wants_description, wants_venue


async def fill_from_event_pages(
    candidates: list[EventCandidate],
    fetch: FetchStrategy,
    fetch_config: FetchConfig,
    *,
    listing_html: str | None = None,
    known_described_urls: frozenset[str] = frozenset(),
    known_venue_urls: frozenset[str] = frozenset(),
    max_fetches: int = DEFAULT_MAX_FETCHES,
) -> tuple[list[EventCandidate], list[str]]:
    """Returns the candidates (those that gained a description or venue
    replaced, the rest unchanged, order kept) and run-level warnings."""
    site_boilerplate: str | None = None
    if listing_html:
        site_boilerplate = meta_description(BeautifulSoup(listing_html, "html.parser"))

    found: dict[str, _PageFindings | None] = {}
    skipped = 0
    for candidate in candidates:
        if max_fetches <= 0:
            break
        page, canonical_url = _lookup_url(candidate)
        if page is None or page in found:
            continue
        if not any(
            _needs(candidate, page, canonical_url, known_described_urls, known_venue_urls)
        ):
            continue
        if len(found) >= max_fetches:
            skipped += 1
            continue
        response = await fetch.fetch(FetchRequest(url=page), fetch_config)
        if response.blocked_reason is not None or response.status_code != 200:
            found[page] = None
            continue
        title = candidate.raw.get("title")
        title = title if isinstance(title, str) else None
        soup = BeautifulSoup(response.text, "html.parser")
        event = _page_event(soup, page)
        found[page] = _PageFindings(
            description=_description(soup, event, title),
            venue=_venue(soup, event, title),
        )

    # A meta description repeated across different pages describes the site,
    # not the event. JSON-LD Event descriptions are per-event by construction
    # (a recurring event's pages may legitimately share one) and are kept.
    meta_counts = Counter(
        f.description[0]
        for f in found.values()
        if f is not None and f.description is not None and f.description[1] != _SOURCE_JSONLD
    )

    described = venued = 0
    result: list[EventCandidate] = []
    for candidate in candidates:
        page, canonical_url = _lookup_url(candidate)
        findings = found.get(page) if page else None
        if findings is None:
            result.append(candidate)
            continue
        wants_description, wants_venue = _needs(
            candidate, page, canonical_url, known_described_urls, known_venue_urls
        )
        raw = dict(candidate.raw)
        paths = dict(candidate.field_source_paths)
        if wants_description and findings.description is not None:
            value, source = findings.description
            boilerplate = source != _SOURCE_JSONLD and (
                meta_counts[value] > 1 or value == site_boilerplate
            )
            if not boilerplate:
                raw["description"] = value
                paths["description"] = source
                described += 1
        if wants_venue and findings.venue is not None:
            page_venue = findings.venue
            changed = False
            if page_venue.venue is not None:
                raw["venue"] = page_venue.venue
                paths["venue"] = page_venue.source
                changed = True
            if page_venue.address is not None and not _has_text(raw.get("address")):
                raw["address"] = page_venue.address
                paths["address"] = page_venue.source
                changed = True
            venued += changed
        if raw != candidate.raw:
            candidate = dataclasses.replace(candidate, raw=raw, field_source_paths=paths)
        result.append(candidate)

    warnings: list[str] = []
    if found:
        warnings.append(f"description_lookup:{described}_filled_of_{len(found)}_pages")
        warnings.append(f"venue_lookup:{venued}_filled_of_{len(found)}_pages")
    if skipped:
        warnings.append(f"event_page_lookup_budget_reached:{skipped}_events_not_looked_up")
    return result, warnings


def _has_text(value: Any) -> bool:
    return isinstance(value, str) and bool(strip_to_text(value))
