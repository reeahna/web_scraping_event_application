"""Fills in an event's missing description, venue and time from its own page.

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

LiveWhale calendars state an event's venue in their own detail block
(`#lw_cal_event_detail_meta`) and often leave it out of the JSON-LD, which
names only the online stream for a hybrid event; that block is read third,
on pages LiveWhale generated.

Time, for an event whose listing gives a bare 12:00 AM with no offset, or
only a date when the page is fetched for a description or venue anyway (and
is not marked all-day):
1. the JSON-LD Event's `startDate`/`endDate`, then
2. the page's `og:start_time` / `og:end_time`.
Only a value with a clock time on the same local day as the listing's date
is used, so a page about another occurrence never moves an event.

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
import re
from collections import Counter
from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
        if venue is not None:
            return PageVenue(venue, address, "detail:jsonld.location")
    found = _microdata_venue(soup, title) or _livewhale_venue(soup, title)
    if found is None and event is not None:
        venue, address = venue_from_location(event.get("location"))
        if address is not None:
            return PageVenue(None, address, "detail:jsonld.location")
    return found


def _is_livewhale(soup: BeautifulSoup) -> bool:
    tag = soup.find("meta", attrs={"name": "generator"})
    return tag is not None and str(tag.get("content", "")).lower().startswith("livewhale")


def _livewhale_venue(soup: BeautifulSoup, title: str | None) -> PageVenue | None:
    """The place line of a LiveWhale event page's detail block: the one item
    there that is not the event's time. Trusted only when there is exactly
    one such item."""
    if not _is_livewhale(soup):
        return None
    block = soup.find(id="lw_cal_event_detail_meta")
    if block is None:
        return None
    items = [
        item
        for item in block.find_all(class_="lw_cal_event_detail_meta_item")
        if item.find("time") is None
    ]
    if len(items) != 1:
        return None
    venue = clean_venue(items[0].get_text(" "), title)
    return PageVenue(venue, None, "detail:livewhale.event_detail_meta") if venue else None


def venue_from_page(html: str, page_url: str, title: str | None = None) -> PageVenue | None:
    """The venue (and address) an event page states for itself, or None."""
    soup = BeautifulSoup(html, "html.parser")
    return _venue(soup, _page_event(soup, page_url), title)


_DATE_ONLY_RE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}\s*$")
_NAIVE_MIDNIGHT_RE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}[T ]00:00(:00(\.0+)?)?\s*$")
_CLOCK_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def time_gap(raw: dict[str, Any]) -> str | None:
    """Why an event's listing gives its date but no usable clock time:
    "placeholder" for a bare midnight with no offset and no end, "date_only"
    for a start with no time at all, None otherwise. All-day events and
    starts that carry an offset are left alone (an explicit UTC midnight is
    7 PM Eastern, not a placeholder)."""
    if raw.get("all_day") is True:
        return None
    if raw.get("start_time") not in (None, ""):
        return None
    start = raw.get("start_datetime")
    if not isinstance(start, str):
        return None
    if _NAIVE_MIDNIGHT_RE.match(start) and not raw.get("end_datetime"):
        return "placeholder"
    if _DATE_ONLY_RE.match(start):
        return "date_only"
    return None


@dataclasses.dataclass(frozen=True)
class PageTime:
    start: str
    end: str | None
    source: str


def _zone(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _local_day(value: str, zone: ZoneInfo | None) -> date | None:
    """The calendar day a page's date-time falls on in the event's zone."""
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is not None and zone is not None:
        moment = moment.astimezone(zone)
    return moment.date()


def _timed(value: Any) -> str | None:
    """A date-time with a real clock time, or None (date-only values and
    naive midnights say no more than the listing did)."""
    if not isinstance(value, str) or not _CLOCK_RE.match(value.strip()):
        return None
    text = value.strip()
    if _NAIVE_MIDNIGHT_RE.match(text):
        return None
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return text


def _page_time(soup: BeautifulSoup, event: dict[str, Any] | None) -> PageTime | None:
    if event is not None:
        start = _timed(event.get("startDate"))
        if start is not None:
            return PageTime(start, _timed(event.get("endDate")), "detail:jsonld.startDate")
    starts = soup.find_all("meta", attrs={"property": "og:start_time"})
    if len(starts) == 1:
        start = _timed(starts[0].get("content"))
        if start is not None:
            end_tag = soup.find("meta", attrs={"property": "og:end_time"})
            end = _timed(end_tag.get("content")) if end_tag is not None else None
            return PageTime(start, end, "detail:meta.og:start_time")
    return None


def time_from_page(html: str, page_url: str) -> PageTime | None:
    """The start (and end) date-time an event page states for itself."""
    soup = BeautifulSoup(html, "html.parser")
    return _page_time(soup, _page_event(soup, page_url))


def _same_occurrence(raw: dict[str, Any], page_time: PageTime, zone: ZoneInfo | None) -> bool:
    """The page's start falls on the listing's own day in the event's zone,
    and its end (if any) does not come before its start."""
    listed = raw.get("start_datetime")
    if not isinstance(listed, str) or len(listed) < 10:
        return False
    try:
        listed_day = date.fromisoformat(listed.strip()[:10])
    except ValueError:
        return False
    if _local_day(page_time.start, zone) != listed_day:
        return False
    if page_time.end is not None:
        try:
            start = datetime.fromisoformat(page_time.start.replace("Z", "+00:00"))
            end = datetime.fromisoformat(page_time.end.replace("Z", "+00:00"))
            if (start.tzinfo is None) != (end.tzinfo is None) or end < start:
                return False
        except ValueError:
            return False
    return True


@dataclasses.dataclass(frozen=True)
class _PageFindings:
    description: tuple[str, str] | None
    venue: PageVenue | None
    time: PageTime | None = None


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
    known_timed_urls: frozenset[str] = frozenset(),
) -> tuple[bool, bool, bool]:
    """(needs a description, needs a venue, needs a time) for this page."""

    def known(urls: frozenset[str]) -> bool:
        return canonical_url in urls or page in urls

    wants_description = not has_description(candidate.raw) and not known(known_described_urls)
    wants_venue = not has_venue(candidate.raw) and not known(known_venue_urls)
    gap = time_gap(candidate.raw)
    # A placeholder midnight is worth a page of its own. A date-only listing
    # is often a genuinely date-only event, so its page is only read for a
    # time when it is being fetched for a description or venue anyway: a
    # source that never gives times doesn't spend the budget every run.
    wants_time = not known(known_timed_urls) and (
        gap == "placeholder" or (gap == "date_only" and (wants_description or wants_venue))
    )
    return wants_description, wants_venue, wants_time


async def fill_from_event_pages(
    candidates: list[EventCandidate],
    fetch: FetchStrategy,
    fetch_config: FetchConfig,
    *,
    listing_html: str | None = None,
    known_described_urls: frozenset[str] = frozenset(),
    known_venue_urls: frozenset[str] = frozenset(),
    known_timed_urls: frozenset[str] = frozenset(),
    timezone: str | None = None,
    max_fetches: int = DEFAULT_MAX_FETCHES,
) -> tuple[list[EventCandidate], list[str]]:
    """Returns the candidates (those that gained a description, venue or time
    replaced, the rest unchanged, order kept) and run-level warnings.
    `timezone` is the events' zone, used to check that a page's time falls
    on the listing's own day."""
    zone = _zone(timezone)
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
            _needs(
                candidate, page, canonical_url,
                known_described_urls, known_venue_urls, known_timed_urls,
            )
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
            time=_page_time(soup, event),
        )

    # A meta description repeated across different pages describes the site,
    # not the event. JSON-LD Event descriptions are per-event by construction
    # (a recurring event's pages may legitimately share one) and are kept.
    meta_counts = Counter(
        f.description[0]
        for f in found.values()
        if f is not None and f.description is not None and f.description[1] != _SOURCE_JSONLD
    )

    described = venued = timed = 0
    result: list[EventCandidate] = []
    for candidate in candidates:
        page, canonical_url = _lookup_url(candidate)
        findings = found.get(page) if page else None
        if findings is None:
            result.append(candidate)
            continue
        wants_description, wants_venue, wants_time = _needs(
            candidate, page, canonical_url,
            known_described_urls, known_venue_urls, known_timed_urls,
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
        if (
            wants_time
            and findings.time is not None
            and _same_occurrence(raw, findings.time, zone)
        ):
            raw["start_datetime"] = findings.time.start
            paths["start_datetime"] = findings.time.source
            if findings.time.end is not None and not raw.get("end_datetime"):
                raw["end_datetime"] = findings.time.end
                paths["end_datetime"] = findings.time.source.replace("startDate", "endDate")
                if paths["end_datetime"].endswith("og:start_time"):
                    paths["end_datetime"] = "detail:meta.og:end_time"
            timed += 1
        if raw != candidate.raw:
            candidate = dataclasses.replace(candidate, raw=raw, field_source_paths=paths)
        result.append(candidate)

    warnings: list[str] = []
    if found:
        warnings.append(f"description_lookup:{described}_filled_of_{len(found)}_pages")
        warnings.append(f"venue_lookup:{venued}_filled_of_{len(found)}_pages")
        if timed:
            warnings.append(f"time_lookup:{timed}_filled_of_{len(found)}_pages")
    if skipped:
        warnings.append(f"event_page_lookup_budget_reached:{skipped}_events_not_looked_up")
    return result, warnings


def _has_text(value: Any) -> bool:
    return isinstance(value, str) and bool(strip_to_text(value))
