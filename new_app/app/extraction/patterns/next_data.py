"""Next.js __NEXT_DATA__ extraction pattern.

Next.js serializes its server-rendered state into a single
`<script id="__NEXT_DATA__" type="application/json">` block. This pattern
parses that strict JSON (never executes anything) and reads the event list
from a config-provided `events_root` path — commonly under
`props.pageProps.<something>`.

There is no default `events_root`: a Next.js site is not necessarily an event
source, and guessing which array in the tree is "the events" is the kind of
heuristic this engine refuses. Without the configured path the pattern
extracts nothing, which is the correct, safe outcome.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from bs4 import BeautifulSoup

from app.extraction.selectors import resolve_json_path
from app.extraction.types import EventCandidate, FetchResponse
from app.schemas.extraction import SiteConfiguration

NAME = "next_data"
PATTERN_VERSION = "1"

_DEFAULT_PATHS: dict[str, str] = {
    "title": "title",
    "description": "description",
    "canonical_url": "url",
    "start_datetime": "startDate",
    "end_datetime": "endDate",
    "venue": "venue",
    "address": "address",
    "image": "image",
    "source_category": "category",
    "external_source_id": "id",
}
_RAW_FIELDS = tuple(_DEFAULT_PATHS.keys())
_EVENTS_ROOT_KEY = "events_root"


def parse_next_data(html: str) -> dict | None:
    """The single __NEXT_DATA__ document, or None. Strict JSON only."""
    soup = BeautifulSoup(html, "html.parser")
    script = soup.find("script", attrs={"id": "__NEXT_DATA__"})
    if script is None:
        return None
    text = script.string or script.get_text()
    if not text or not text.strip():
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _events_from(document: dict, events_root: str | None) -> list[dict]:
    if not events_root:
        return []
    resolved = resolve_json_path(document, events_root).value
    return [e for e in resolved if isinstance(e, dict)] if isinstance(resolved, list) else []


class NextDataPattern:
    name = NAME

    def extract(self, response: FetchResponse, config: SiteConfiguration) -> list[EventCandidate]:
        document = parse_next_data(response.text)
        if document is None:
            return []
        events_root = config.json_paths.get(_EVENTS_ROOT_KEY)
        paths = {
            **_DEFAULT_PATHS,
            **{k: v for k, v in config.json_paths.items() if k in _RAW_FIELDS},
        }

        candidates: list[EventCandidate] = []
        for index, event in enumerate(_events_from(document, events_root)):
            raw: dict[str, Any] = {}
            field_source_paths: dict[str, str] = {}
            for field_name, path in paths.items():
                result = resolve_json_path(event, path)
                raw[field_name] = result.value
                field_source_paths[field_name] = f"__NEXT_DATA__.{events_root}[{index}].{path}"

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
