"""Nuxt payload extraction pattern.

Nuxt exposes server state either as a strict JSON `_payload.json` endpoint
(fetched directly, the preferred case) or, on some builds, only as a
JavaScript `window.__NUXT__ = (function(...){...})(...)` assignment that can be
reconstructed *only by executing JavaScript*. This pattern handles the first
case and deliberately refuses the second: if the page has no parseable strict
JSON payload, it extracts nothing, and detection marks the source
`browser_required` so it is deferred to the Phase 9 restricted-browser path
rather than being wrongly parsed here. Nothing is ever eval'd.

Field mapping is config-driven `json_paths` with an `events_root`, same as the
other JSON patterns. `config.api_endpoint` pointed at `_payload.json` is the
normal configuration; a raw JSON body is parsed directly, otherwise a
`<script type="application/json">` payload block is used.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from bs4 import BeautifulSoup

from app.extraction.selectors import resolve_json_path
from app.extraction.types import EventCandidate, FetchResponse
from app.schemas.extraction import SiteConfiguration

NAME = "nuxt_payload"
PATTERN_VERSION = "1"

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


def parse_nuxt_payload(text: str) -> Any | None:
    """A strict-JSON Nuxt payload from a direct body or an application/json
    script. Returns None when the only state is a JS assignment — which the
    caller treats as browser_required, never as something to eval."""
    stripped = text.strip()
    if stripped[:1] in ("{", "["):
        try:
            return json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            pass
    soup = BeautifulSoup(text, "html.parser")
    # Only the Nuxt-specific payload script — never a generic application/json
    # block, which belongs to the embedded_json pattern. Matching that here
    # would make every embedded-JSON page look like Nuxt.
    script = soup.find("script", attrs={"id": "__NUXT_DATA__"})
    if script is not None:
        body = script.string or script.get_text()
        if body and body.strip():
            try:
                return json.loads(body)
            except (json.JSONDecodeError, ValueError):
                return None
    return None


def _events_from(document: Any, events_root: str | None) -> list[dict]:
    if events_root:
        resolved = resolve_json_path(document, events_root).value
        return [e for e in resolved if isinstance(e, dict)] if isinstance(resolved, list) else []
    if isinstance(document, list):
        return [e for e in document if isinstance(e, dict)]
    return []


class NuxtPayloadPattern:
    name = NAME

    def extract(self, response: FetchResponse, config: SiteConfiguration) -> list[EventCandidate]:
        document = parse_nuxt_payload(response.text)
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
                field_source_paths[field_name] = f"nuxt.{events_root or ''}[{index}].{path}"

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
