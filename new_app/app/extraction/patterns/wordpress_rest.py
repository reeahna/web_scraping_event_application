"""WordPress REST API extraction pattern.

Fetches `config.api_endpoint` expecting a JSON array of post-like objects.
Field mapping is entirely config-driven (`config.json_paths`) — this does
not assume any single fixed events plugin. A post is only usable as an
event if the configured date-field mapping resolves to a real value; a post
without one simply fails EventValidator's required-start-date check
downstream, exactly like any other pattern's data-shape mismatch — no
separate "is this really an event" heuristic is needed here.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.extraction.selectors import resolve_json_path
from app.extraction.types import EventCandidate, FetchResponse
from app.schemas.extraction import SiteConfiguration

NAME = "wordpress_rest"
PATTERN_VERSION = "1"

_DEFAULT_PATHS: dict[str, str] = {
    "title": "title.rendered",
    "description": "content.rendered",
    "canonical_url": "link",
    "start_datetime": "date_gmt",
    "external_source_id": "id",
    "source_category": "type",
}

_RAW_FIELDS = (
    "title",
    "description",
    "canonical_url",
    "start_datetime",
    "end_datetime",
    "venue",
    "address",
    "image",
    "source_category",
    "external_source_id",
)


class WordPressRestPattern:
    name = NAME

    def extract(self, response: FetchResponse, config: SiteConfiguration) -> list[EventCandidate]:
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError:
            return []
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list):
            return []

        paths = {
            **_DEFAULT_PATHS,
            **{k: v for k, v in config.json_paths.items() if k in _RAW_FIELDS},
        }

        candidates: list[EventCandidate] = []
        for post in payload:
            if not isinstance(post, dict):
                continue

            raw: dict[str, Any] = {}
            field_source_paths: dict[str, str] = {}
            for field_name, path in paths.items():
                result = resolve_json_path(post, path)
                raw[field_name] = result.value
                field_source_paths[field_name] = result.source_path or f"jsonpath:{path}"

            if raw.get("external_source_id") is not None:
                raw["external_source_id"] = str(raw["external_source_id"])

            raw_record_hash = hashlib.sha256(
                json.dumps(post, sort_keys=True, default=str).encode("utf-8")
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
