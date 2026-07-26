"""Embedded JSON extraction pattern.

Extracts events from strict, non-executable JSON already present in the page:
`<script type="application/json">` blocks and (via config) a named element's
JSON-bearing attribute or text. Field mapping is config-driven `json_paths`,
the same convention as every other JSON pattern.

Strictly non-executable. This never parses `window.x = {...}` assignments, never
runs JavaScript, and never evals — only `json.loads` on a `<script>` whose type
is a JSON MIME type. `application/ld+json` is skipped here because the
`json_ld_event` pattern owns schema.org; a site that only has JSON-LD should be
detected as that, not this.

`config.json_paths` must include an `events_root` entry naming the path (within
each parsed JSON document) to the list of event objects; without it there is no
way to know which array in an arbitrary document is the events, so the pattern
extracts nothing rather than guessing.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from bs4 import BeautifulSoup

from app.extraction.selectors import resolve_json_path
from app.extraction.types import EventCandidate, FetchResponse
from app.schemas.extraction import SiteConfiguration

NAME = "embedded_json"
PATTERN_VERSION = "1"

_JSON_SCRIPT_TYPES = ("application/json",)

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

# Where in each parsed document the event list lives. Config-provided; there is
# no default, because guessing which array is "the events" is exactly the kind
# of heuristic this engine avoids.
_EVENTS_ROOT_KEY = "events_root"


def _parse_json_scripts(html: str) -> list[Any]:
    soup = BeautifulSoup(html, "html.parser")
    documents: list[Any] = []
    for script in soup.find_all("script", attrs={"type": _JSON_SCRIPT_TYPES}):
        text = script.string or script.get_text()
        if not text or not text.strip():
            continue
        try:
            documents.append(json.loads(text))
        except (json.JSONDecodeError, ValueError):
            continue
    return documents


def _events_from(document: Any, events_root: str | None) -> list[dict]:
    if events_root:
        resolved = resolve_json_path(document, events_root).value
        return [e for e in resolved if isinstance(e, dict)] if isinstance(resolved, list) else []
    # No configured root: only accept a document that is *itself* a list of
    # event-like objects, never a guess into a nested structure.
    if isinstance(document, list):
        return [e for e in document if isinstance(e, dict)]
    return []


class EmbeddedJsonPattern:
    name = NAME

    def extract(self, response: FetchResponse, config: SiteConfiguration) -> list[EventCandidate]:
        events_root = config.json_paths.get(_EVENTS_ROOT_KEY)
        paths = {
            **_DEFAULT_PATHS,
            **{k: v for k, v in config.json_paths.items() if k in _RAW_FIELDS},
        }

        candidates: list[EventCandidate] = []
        for document in _parse_json_scripts(response.text):
            for index, event in enumerate(_events_from(document, events_root)):
                raw: dict[str, Any] = {}
                field_source_paths: dict[str, str] = {}
                for field_name, path in paths.items():
                    result = resolve_json_path(event, path)
                    raw[field_name] = result.value
                    field_source_paths[field_name] = f"embedded_json[{index}].{path}"

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
