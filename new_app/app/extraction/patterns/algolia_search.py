"""Algolia search-index extraction pattern.

Extracts events from an Algolia query response — `{"hits": [...], "nbHits":
..., "nbPages": ..., "page": ...}` — mapping each hit via `config.json_paths`,
the same convention as the other JSON patterns. `objectID` is the stable
identity.

Key handling is the whole point of care here. A raw Algolia key never lives in
a `SiteConfiguration`: the *only* accepted key is a search-only key supplied
through a secret reference in `fetch.secret_header_refs`
(`X-Algolia-API-Key -> env:...`), resolved at request time and never stored,
logged, or audited (see app.services.secrets and app.extraction.fetch). An
admin/write key must never be referenced; because nothing is ever persisted,
no key of any kind is stored. When no key reference is configured the source
is left in review and no query is made (see the proposer).

This module parses the response only; the authenticated POST is performed by
the ordinary fetch pipeline using the configured endpoint, body, app-id header
and the resolved key header.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.extraction.selectors import resolve_json_path
from app.extraction.types import EventCandidate, FetchResponse
from app.schemas.extraction import SiteConfiguration

NAME = "algolia_search"
PATTERN_VERSION = "1"

_DEFAULT_PATHS: dict[str, str] = {
    "external_source_id": "objectID",
    "title": "title",
    "description": "description",
    "canonical_url": "url",
    "start_datetime": "start",
    "end_datetime": "end",
    "venue": "venue",
    "address": "address",
    "image": "image",
    "source_category": "category",
}
_RAW_FIELDS = tuple(_DEFAULT_PATHS.keys())
_HITS_ROOT_DEFAULT = "hits"


class AlgoliaSearchPattern:
    name = NAME

    def extract(self, response: FetchResponse, config: SiteConfiguration) -> list[EventCandidate]:
        try:
            payload = json.loads(response.text)
        except (json.JSONDecodeError, ValueError):
            return []
        if not isinstance(payload, dict):
            return []

        hits_root = config.json_paths.get("events_root", _HITS_ROOT_DEFAULT)
        resolved = resolve_json_path(payload, hits_root).value
        hits = [h for h in resolved if isinstance(h, dict)] if isinstance(resolved, list) else []

        paths = {
            **_DEFAULT_PATHS,
            **{k: v for k, v in config.json_paths.items() if k in _RAW_FIELDS},
        }

        candidates: list[EventCandidate] = []
        for index, hit in enumerate(hits):
            raw: dict[str, Any] = {}
            field_source_paths: dict[str, str] = {}
            for field_name, path in paths.items():
                result = resolve_json_path(hit, path)
                raw[field_name] = result.value
                field_source_paths[field_name] = f"algolia.hits[{index}].{path}"
            if raw.get("external_source_id") is not None:
                raw["external_source_id"] = str(raw["external_source_id"])

            raw_record_hash = hashlib.sha256(
                json.dumps(hit, sort_keys=True, default=str).encode("utf-8")
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
