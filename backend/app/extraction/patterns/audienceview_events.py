"""AudienceView box-office listing pattern.

Box offices that run AudienceView Unlimited (for example a university
performing-arts center's "Upcoming events" page under `/Online/default.asp`)
server-render their whole listing into a script as a column table:

    var articleContext = {
        searchHeaders : [ "Id", "Object Type", ..., "Short Description",
                          "Start Date", ... ],
        searchResults : [ [ "D7D2...", "P", ..., "Stella Cole Quartet",
                            "Thursday, December 3, 2026 / 7:00 PM", ... ], ... ],
        ...
    };

The page's own JavaScript turns that table into cards, which is why a plain
HTML-card scrape of the HTTP response finds nothing. The table itself is already
in the response, so no browser is needed.

Strictly non-executable, like `inline_json_events`: this never runs JavaScript
and never evals. It finds the `searchHeaders` and `searchResults` literals
textually, takes the balanced array that follows each, and parses them with
`json.loads`. The only rewrite before parsing is turning the JavaScript `\\'`
escape (an apostrophe inside a double-quoted string, which JSON does not allow)
back into a plain `'`. Anything else that is not valid JSON yields nothing.

Each row becomes a record holding every column by header name plus an `event`
object with the fields derived here (title, start, canonical URL pulled from the
"MORE INFO" link, venue, address, ...). The default `json_paths` point at that
`event` object; a configuration can point any field at a raw column instead
(for example `"description": "Data 2"`).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup

from app.extraction.patterns.inline_json import _balanced_literal
from app.extraction.selectors import resolve_json_path
from app.extraction.types import EventCandidate, FetchResponse
from app.schemas.extraction import SiteConfiguration

NAME = "audienceview_events"
PATTERN_VERSION = "1"

_HEADERS_RE = re.compile(r"\bsearchHeaders\s*:\s*(?=\[)")
_RESULTS_RE = re.compile(r"\bsearchResults\s*:\s*(?=\[)")
# A JS `\'` escape that is not itself an escaped backslash (`\\'`).
_JS_APOSTROPHE_RE = re.compile(r"(?<!\\)((?:\\\\)*)\\'")
# Bound on how many listing tables one page is scanned for.
_MAX_TABLES = 10
# The columns that make a table an AudienceView event listing rather than some
# other header/row structure.
_REQUIRED_HEADERS = frozenset({"Id", "Start Date"})
_TITLE_HEADERS = ("Short Description", "Data 1")
# AudienceView object type for a single performance. Other object types
# (packages, bundles, gift certificates) are not events and are skipped. Rows
# with no object type at all are kept.
_PERFORMANCE_TYPE = "P"

_DEFAULT_PATHS: dict[str, str] = {
    "title": "event.title",
    "description": "event.description",
    "canonical_url": "event.url",
    "start_datetime": "event.start",
    "venue": "event.venue",
    "address": "event.address",
    "image": "event.image",
    "source_category": "event.category",
    "external_source_id": "event.id",
    "latitude": "event.latitude",
    "longitude": "event.longitude",
}
_RAW_FIELDS = tuple(_DEFAULT_PATHS.keys())


@dataclass(frozen=True)
class AudienceViewListing:
    """Every performance row found in the page, as header-keyed records, in
    page order with repeated performance ids removed."""

    headers: tuple[str, ...]
    records: list[dict[str, Any]]


def _parse_array(text: str, start: int) -> list | None:
    literal = _balanced_literal(text, start)
    if literal is None:
        return None
    try:
        value = json.loads(_JS_APOSTROPHE_RE.sub(r"\1'", literal))
    except (json.JSONDecodeError, ValueError):
        return None
    return value if isinstance(value, list) else None


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, (list, dict)):
        return None
    text = str(value).strip()
    return text or None


def _first(columns: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = _text(columns.get(name))
        if value:
            return value
    return None


def _link_from(markup: str | None) -> str | None:
    """The first link in a column holding an HTML snippet such as
    `<a href="https://...">MORE INFO</a>`."""
    if not markup or "<a" not in markup.lower():
        return None
    anchor = BeautifulSoup(markup, "html.parser").find("a", href=True)
    href = anchor.get("href", "").strip() if anchor else ""
    if not href or href.lower().startswith(("javascript:", "#", "mailto:", "tel:")):
        return None
    return href


def _start_from(columns: dict[str, Any]) -> str | None:
    """A naive local ISO date-time for the performance.

    The display column ("Thursday, December 3, 2026 / 7:00 PM") is read first
    because it is unambiguous. Failing that, the split columns are used; their
    month is zero-based (December is "11"), as JavaScript's Date month is."""
    display = _text(columns.get("Start Date"))
    if display:
        parsed = _parse_display_date(display)
        if parsed:
            return parsed
    year = _text(columns.get("Start Date year"))
    month = _text(columns.get("Start Date month"))
    day = _text(columns.get("Start Date date"))
    if not (year and month and day):
        return None
    try:
        date_value = datetime(int(year), int(month) + 1, int(day))
    except (ValueError, OverflowError):
        return None
    time_text = _text(columns.get("Start Date time"))
    if time_text:
        try:
            clock = datetime.strptime(time_text, "%H:%M")
        except ValueError:
            return date_value.date().isoformat()
        return date_value.replace(hour=clock.hour, minute=clock.minute).isoformat()
    return date_value.date().isoformat()


def _parse_display_date(display: str) -> str | None:
    # Drop the leading weekday ("Thursday, "), then split date and time on "/".
    text = re.sub(r"^[A-Za-z]+,\s*", "", display.strip())
    date_part, _, time_part = (p.strip() for p in text.partition("/"))
    try:
        date_value = datetime.strptime(date_part, "%B %d, %Y")
    except ValueError:
        return None
    if time_part:
        try:
            clock = datetime.strptime(time_part.upper().replace(".", ""), "%I:%M %p")
        except ValueError:
            return date_value.date().isoformat()
        return date_value.replace(hour=clock.hour, minute=clock.minute).isoformat()
    return date_value.date().isoformat()


def _description_from(columns: dict[str, Any], title: str | None) -> str | None:
    meta = _first(columns, "Meta Description")
    if meta:
        return meta
    lines = [
        line
        for line in (_text(columns.get("Data 1")), _text(columns.get("Data 2")))
        if line and line != title
    ]
    return " ".join(lines) or None


def _derived_event(columns: dict[str, Any]) -> dict[str, Any]:
    title = _first(columns, *_TITLE_HEADERS)
    return {
        "id": _first(columns, "Id"),
        "title": title,
        "description": _description_from(columns, title),
        "url": _link_from(_text(columns.get("Description")))
        or _link_from(_text(columns.get("Additional Info"))),
        "start": _start_from(columns),
        "venue": _first(columns, "Venue Description", "Venue Name"),
        "address": _first(columns, "Venue Data 10", "Data 10", "Street"),
        "image": _first(columns, "Image 1", "thumbnail"),
        "category": _first(columns, "filter1", "Category"),
        "latitude": _first(columns, "Latitude"),
        "longitude": _first(columns, "Longitude"),
    }


def find_audienceview_listing(html: str) -> AudienceViewListing | None:
    """Every AudienceView listing table in the page, merged. None when the page
    holds no table with the identifying columns."""
    headers_out: tuple[str, ...] = ()
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    tables = 0
    for match in _HEADERS_RE.finditer(html):
        if tables >= _MAX_TABLES:
            break
        headers = _parse_array(html, match.end())
        if not headers or not all(isinstance(h, str) for h in headers):
            continue
        if not _REQUIRED_HEADERS.issubset(headers):
            continue
        results_match = _RESULTS_RE.search(html, match.end())
        if results_match is None:
            continue
        rows = _parse_array(html, results_match.end())
        if rows is None:
            continue
        tables += 1
        headers_out = headers_out or tuple(headers)
        for row in rows:
            if not isinstance(row, list):
                continue
            columns = {
                header: row[i] for i, header in enumerate(headers) if header and i < len(row)
            }
            object_type = _text(columns.get("Object Type"))
            if object_type and object_type != _PERFORMANCE_TYPE:
                continue
            event = _derived_event(columns)
            if event["id"]:
                if event["id"] in seen_ids:
                    continue
                seen_ids.add(event["id"])
            records.append({**columns, "event": event})
    if not tables:
        return None
    return AudienceViewListing(headers=headers_out, records=records)


class AudienceViewEventsPattern:
    name = NAME

    def extract(self, response: FetchResponse, config: SiteConfiguration) -> list[EventCandidate]:
        listing = find_audienceview_listing(response.text)
        if listing is None:
            return []
        paths = {
            **_DEFAULT_PATHS,
            **{k: v for k, v in config.json_paths.items() if k in _RAW_FIELDS},
        }

        candidates: list[EventCandidate] = []
        for index, record in enumerate(listing.records):
            raw: dict[str, Any] = {}
            field_source_paths: dict[str, str] = {}
            for field_name, path in paths.items():
                raw[field_name] = resolve_json_path(record, path).value
                field_source_paths[field_name] = f"searchResults[{index}].{path}"

            if raw.get("external_source_id") is not None:
                raw["external_source_id"] = str(raw["external_source_id"])

            raw_record_hash = hashlib.sha256(
                json.dumps(record, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()

            candidates.append(
                EventCandidate(
                    raw=raw,
                    title=None,
                    canonical_url=None,
                    description=None,
                    start_date=None,
                    start_time=None,
                    end_date=None,
                    end_time=None,
                    timezone=None,
                    venue=None,
                    address=None,
                    image_url=None,
                    latitude=None,
                    longitude=None,
                    source_category=None,
                    external_source_id=None,
                    field_source_paths=field_source_paths,
                    transformation_history=(),
                    source_page=response.final_url,
                    extraction_pattern=NAME,
                    warnings=(),
                    raw_record_hash=raw_record_hash,
                )
            )
        return candidates
