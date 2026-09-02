"""Inline JSON-variable extraction pattern.

Some sites embed their event list as a JSON literal assigned to a JavaScript
variable — e.g. FullCalendar-driven pages that server-render
`window.eventsListing = [{...}, ...]`. The data is already present; only the
`<script type="application/json">` framing `embedded_json` requires is missing.

Strictly non-executable, exactly like `embedded_json`: this NEVER runs
JavaScript and NEVER evals. It locates the assignment textually, extracts the
balanced array/object literal that follows, and runs `json.loads` on it. A
literal that is not valid JSON (single-quoted keys, trailing commas, function
calls) simply does not parse and yields nothing — there is no fallback that
executes anything.

`config.json_paths["events_root"]` names the variable holding the event list
(e.g. `eventsListing`); field mapping is the same config-driven `json_paths`
convention as every other JSON pattern and supports dotted paths into nested
objects (e.g. `extendedProps.buyUrl`).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from app.extraction.selectors import resolve_json_path
from app.extraction.types import EventCandidate, FetchResponse
from app.schemas.extraction import SiteConfiguration

NAME = "inline_json_events"
PATTERN_VERSION = "1"

# A JS assignment whose right-hand side opens an array/object literal:
# `window.eventsListing = [ ... ]`, `var data = { ... }`, etc.
_VAR_ASSIGN_RE = re.compile(
    r"(?:window\.|globalThis\.|var\s+|let\s+|const\s+)?([A-Za-z_$][\w$]*)\s*=\s*(?=[\[{])"
)
# Bound on how many assignments a single page is scanned for, so a pathological
# script can't make detection expensive.
_MAX_ASSIGNMENTS = 40
# Shortest literal worth parsing — avoids `x = []` style noise.
_MIN_LITERAL_CHARS = 40


@dataclass(frozen=True)
class InlineEventVariable:
    """The best event-like JS-variable array found in a page: its name, the
    event records it holds, and how event-like they are. Shared by the detector
    and the proposer so they always agree on the same variable."""

    name: str
    records: list[dict]
    event_like_rate: float
    size: int


def find_inline_event_variable(html: str) -> InlineEventVariable | None:
    """Scan the page's JS-variable assignments for the highest-scoring array of
    event-like objects. Parses each literal with json.loads only — never
    executes. Returns None when nothing clears the event-likeness bar."""
    from app.extraction.inference.json_events import find_event_arrays

    best: InlineEventVariable | None = None
    best_score = -1.0
    evaluated = 0
    for match in _VAR_ASSIGN_RE.finditer(html):
        if evaluated >= _MAX_ASSIGNMENTS:
            break
        literal = _balanced_literal(html, match.end())
        if literal is None or len(literal) < _MIN_LITERAL_CHARS:
            continue
        evaluated += 1
        try:
            document = json.loads(literal)
        except (json.JSONDecodeError, ValueError):
            continue
        arrays = find_event_arrays(document)
        if not arrays:
            continue
        candidate = arrays[0]
        if candidate.score > best_score:
            best_score = candidate.score
            best = InlineEventVariable(
                name=match.group(1),
                records=_events_from(document),
                event_like_rate=candidate.event_like_rate,
                size=candidate.size,
            )
    return best

_DEFAULT_PATHS: dict[str, str] = {
    "title": "title",
    "description": "description",
    "canonical_url": "url",
    "start_datetime": "start",
    "end_datetime": "end",
    "venue": "venue",
    "address": "address",
    "image": "image",
    "source_category": "category",
    "external_source_id": "id",
}
_RAW_FIELDS = tuple(_DEFAULT_PATHS.keys())

_EVENTS_ROOT_KEY = "events_root"

_OPEN_TO_CLOSE = {"[": "]", "{": "}"}


def _balanced_literal(text: str, start: int) -> str | None:
    """Return the balanced JSON array/object literal beginning at or after
    `start`, honoring string quoting so brackets inside strings don't count.
    None when the next non-space character isn't `[`/`{` or the literal never
    closes."""
    i = start
    n = len(text)
    while i < n and text[i] in " \t\r\n":
        i += 1
    if i >= n or text[i] not in _OPEN_TO_CLOSE:
        return None
    open_ch = text[i]
    close_ch = _OPEN_TO_CLOSE[open_ch]
    depth = 0
    in_str = False
    quote = ""
    escaped = False
    j = i
    while j < n:
        c = text[j]
        if in_str:
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == quote:
                in_str = False
        elif c in "\"'":
            in_str = True
            quote = c
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return text[i : j + 1]
        j += 1
    return None


def parse_inline_json_var(html: str, var_name: str) -> Any | None:
    """The value of the first JS assignment `<var_name> = <json-literal>` in the
    page, parsed with `json.loads`. `var_name` may be given with or without a
    `window.` prefix. Returns None when it isn't present or isn't valid JSON."""
    bare = var_name.split(".")[-1]
    pattern = re.compile(
        r"(?:window\.|globalThis\.|var\s+|let\s+|const\s+)?"
        + re.escape(bare)
        + r"\s*=\s*",
    )
    for match in pattern.finditer(html):
        literal = _balanced_literal(html, match.end())
        if literal is None:
            continue
        try:
            return json.loads(literal)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _events_from(value: Any) -> list[dict]:
    if isinstance(value, list):
        return [e for e in value if isinstance(e, dict)]
    if isinstance(value, dict):
        # A wrapper object: accept the first list-of-objects it directly holds.
        for candidate in value.values():
            if isinstance(candidate, list) and any(isinstance(e, dict) for e in candidate):
                return [e for e in candidate if isinstance(e, dict)]
    return []


class InlineJsonEventsPattern:
    name = NAME

    def extract(self, response: FetchResponse, config: SiteConfiguration) -> list[EventCandidate]:
        var_name = config.json_paths.get(_EVENTS_ROOT_KEY)
        if not var_name:
            return []
        value = parse_inline_json_var(response.text, var_name)
        if value is None:
            return []

        paths = {
            **_DEFAULT_PATHS,
            **{k: v for k, v in config.json_paths.items() if k in _RAW_FIELDS},
        }

        candidates: list[EventCandidate] = []
        for index, event in enumerate(_events_from(value)):
            raw: dict[str, Any] = {}
            field_source_paths: dict[str, str] = {}
            for field_name, path in paths.items():
                result = resolve_json_path(event, path)
                raw[field_name] = result.value
                field_source_paths[field_name] = f"{var_name}[{index}].{path}"

            if raw.get("external_source_id") is not None:
                raw["external_source_id"] = str(raw["external_source_id"])

            raw_record_hash = hashlib.sha256(
                json.dumps(event, sort_keys=True, default=str).encode("utf-8")
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
