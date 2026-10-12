"""Paged JSON events API extraction pattern.

Many first-party event APIs (Spring Data `Page` responses, and the many
hand-written APIs that copy that shape) return one page of records in a top
level array — by default `content` — next to page metadata such as
`totalPages`, `totalElements`, `number`, `size`, `last` and `pageable`:

    {"content": [{"id": 1, "title": "...", "startDate": "...", ...}, ...],
     "number": 0, "size": 10, "totalPages": 7, "totalElements": 64, "last": false}

Record field names vary between deployments, so this pattern does not assume
them. `infer_field_paths` picks each field's path from a closed list of common
key names (looking one level into nested objects, and into the first element of
a nested list such as `instances[0].start`); the proposer stores the chosen
paths in `config.json_paths`, where an administrator can override any of them.
The record-array root is `config.json_paths["events_root"]`.

When the start date sits inside a nested list (one record, many dated
instances), each instance becomes its own candidate, bounded per record.

Like every pattern, `extract` fills only `raw` + provenance. The one
conversion done here is turning machine timestamps (epoch numbers, Jackson
`[y, m, d, h, min]` arrays, offset-bearing ISO strings) into local wall-clock
ISO strings in the configured timezone, because normalize stores the wall-clock
time it is given and would otherwise keep UTC times as local ones.

Nothing here references a hostname, domain or institution.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.extraction.selectors import resolve_json_path
from app.extraction.types import EventCandidate, FetchResponse
from app.schemas.extraction import SiteConfiguration

NAME = "paged_json_events"
PATTERN_VERSION = "1"

EVENTS_ROOT_DEFAULT = "content"
# Top-level keys a paged API may put its record array under. `content` is the
# Spring Data name; the others only count when page metadata sits beside them.
RECORD_ROOTS = ("content", "items", "results", "records", "data")
# Page metadata keys, compared case/punctuation-insensitively.
PAGE_METADATA_KEYS = frozenset(
    {
        "totalpages", "totalelements", "number", "size", "last", "first",
        "numberofelements", "pageable", "page", "pagenumber", "currentpage",
        "pagesize", "pagecount", "totalitems", "totalcount", "total", "hasnext",
    }
)

# Candidate key names per field, most-preferred first. Compared after
# lower-casing and dropping non-alphanumerics, so start_date == startDate.
_FIELD_KEYS: dict[str, tuple[str, ...]] = {
    "external_source_id": ("id", "eventid", "uuid", "uid", "guid", "eventuuid"),
    "title": ("title", "name", "eventname", "eventtitle", "headline"),
    "start_datetime": (
        "startdatetime", "startdate", "start", "startsat", "starttime", "eventstart",
        "begindate", "begin", "datestart", "startdateutc", "eventdate", "date", "firstdate",
        "datetime", "dates", "occurrences",
    ),
    "end_datetime": (
        "enddatetime", "enddate", "end", "endsat", "endtime", "eventend", "dateend",
        "finishdate", "lastdate", "datetime",
    ),
    "canonical_url": (
        "url", "eventurl", "permalink", "link", "detailurl", "canonicalurl", "weburl",
        "htmlurl", "href", "pageurl", "absoluteurl",
    ),
    "description": (
        "description", "summary", "body", "details", "teaser", "excerpt", "shortdescription",
    ),
    "venue": ("location", "venue", "locationname", "venuename", "place", "building"),
    "address": ("address", "locationaddress", "streetaddress", "fulladdress"),
    "image": (
        "image", "imageurl", "photo", "photourl", "thumbnail", "featuredimage", "media", "img",
    ),
    "categories": ("categories", "category", "eventtypes", "types", "tags", "keywords"),
    "latitude": ("latitude", "lat"),
    "longitude": ("longitude", "lng", "lon", "long"),
}
# Fields that may be taken from inside a nested list's first element (an
# instance/occurrence list). Identity and title must be the record's own.
_LIST_NESTABLE = frozenset({"start_datetime", "end_datetime", "venue", "address"})
# Fields that are always the record's own top-level value.
_TOP_LEVEL_ONLY = frozenset({"external_source_id", "title"})
# Fields whose value may itself be an object the extractor unwraps.
_OBJECT_VALUED = frozenset({"venue", "address", "image", "categories"})
FIELD_NAMES = tuple(_FIELD_KEYS)

MAX_INSTANCES_PER_RECORD = 50

_IMAGE_DICT_KEYS = ("url", "src", "href", "large", "original", "medium", "small", "uri")
_NAME_KEYS = ("name", "title", "label", "displayName", "value")
_UNSAFE_URL_PREFIXES = ("javascript:", "data:", "file:", "vbscript:", "blob:")


def norm_key(key: Any) -> str:
    return "".join(ch for ch in str(key).lower() if ch.isalnum())


def record_root(payload: Any) -> str | None:
    """The top-level key holding the record array of a paged response, or
    None. `content` qualifies on its own; the more generic names only count
    next to page metadata, so an unrelated `{"data": [...]}` is not claimed."""
    if not isinstance(payload, dict):
        return None
    has_metadata = any(norm_key(k) in PAGE_METADATA_KEYS for k in payload)
    for key in RECORD_ROOTS:
        value = payload.get(key)
        if not isinstance(value, list):
            continue
        if key == "content" or has_metadata:
            return key
    return None


def records_at(payload: Any, root: str) -> list[dict]:
    resolved = resolve_json_path(payload, root).value
    return [r for r in resolved if isinstance(r, dict)] if isinstance(resolved, list) else []


def _present(value: Any) -> bool:
    return value not in (None, "", [], {})


def _is_container(value: Any, field: str) -> bool:
    """An object or list value — except a Jackson date array, which is a list
    of ints standing for one timestamp."""
    if field in ("start_datetime", "end_datetime") and isinstance(value, list):
        return not all(isinstance(part, int) for part in value)
    return isinstance(value, (dict, list))


def _scalar_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, (str, int, float)) and not isinstance(item, bool)
                for item in value)
        and not all(isinstance(item, int) for item in value)
    )


def _path_candidates(record: dict, field: str) -> list[str]:
    """Paths in `record` whose final key is one of `field`'s candidate names,
    in preference order: the record's own keys, then one object level down,
    then (for instance-like fields) the first element of a nested list."""
    wanted = _FIELD_KEYS[field]
    rank = {name: index for index, name in enumerate(wanted)}
    found: list[tuple[int, int, str]] = []
    for key, value in record.items():
        nk = norm_key(key)
        if nk in rank and _present(value) and (
            field in _OBJECT_VALUED or not _is_container(value, field)
        ):
            found.append((0, rank[nk], str(key)))
        elif nk in rank and field in _LIST_NESTABLE and _scalar_list(value):
            # A list of plain timestamps (`"dates": ["2026-10-12T19:00", ...]`):
            # each entry is one instance.
            found.append((2, rank[nk], f"{key}.0"))
    if field in _TOP_LEVEL_ONLY:
        found.sort()
        return [path for _, _, path in found]
    for key, value in record.items():
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                nk = norm_key(sub_key)
                if nk in rank and _present(sub_value) and not isinstance(sub_value, dict):
                    found.append((1, rank[nk], f"{key}.{sub_key}"))
        elif (
            field in _LIST_NESTABLE
            and isinstance(value, list)
            and value
            and isinstance(value[0], dict)
        ):
            for sub_key, sub_value in value[0].items():
                nk = norm_key(sub_key)
                if nk in rank and _present(sub_value):
                    found.append((2, rank[nk], f"{key}.0.{sub_key}"))
    found.sort()
    return [path for _, _, path in found]


def infer_field_paths(records: list[dict]) -> dict[str, str]:
    """The best path for each field across `records`: the candidate present in
    the most records, ties broken by preference order. Deterministic."""
    paths: dict[str, str] = {}
    sample = records[:25]
    for field in FIELD_NAMES:
        counts: dict[str, int] = {}
        order: dict[str, tuple[int, int]] = {}
        for record_index, record in enumerate(sample):
            for position, path in enumerate(_path_candidates(record, field)):
                counts[path] = counts.get(path, 0) + 1
                order.setdefault(path, (position, record_index))
        if not counts:
            continue
        best = sorted(counts, key=lambda p: (-counts[p], order[p]))[0]
        if field == "canonical_url" and not _looks_like_url_field(sample, best):
            continue
        paths[field] = best
    # A start taken from an instance list and an end from elsewhere (or vice
    # versa) would mix two different things; keep the end only when it comes
    # from the same place as the start.
    start, end = paths.get("start_datetime"), paths.get("end_datetime")
    if start and end and _instance_list(start) != _instance_list(end):
        paths.pop("end_datetime")
    return paths


def _looks_like_url_field(records: list[dict], path: str) -> bool:
    """A URL field must hold URLs or root-relative paths. A bare slug cannot be
    turned into a link without guessing the site's route, so it is not used."""
    for record in records:
        value = resolve_json_path(record, path).value
        if isinstance(value, str) and value.strip():
            text = value.strip().lower()
            return text.startswith(("http://", "https://", "/"))
    return False


def _instance_list(path: str) -> str | None:
    """`instances` for `instances.0.start` (or `dates.0`, a list of plain
    timestamps); None for a path not through a list."""
    parts = path.split(".")
    if len(parts) >= 2 and parts[1] == "0":
        return parts[0]
    return None


def _zone(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def local_datetime_string(value: Any, timezone: str | None) -> Any:
    """Machine timestamps as a local wall-clock ISO string; anything else
    unchanged (date-only strings, local times and prose are normalize's job).

    * epoch seconds or milliseconds (int/float, or a digit string),
    * Jackson `LocalDateTime` arrays: [2026, 10, 12] or [2026, 10, 12, 19, 30],
    * ISO strings carrying `Z` or an offset — converted to `timezone`.
    """
    zone = _zone(timezone)
    moment: datetime | None = None
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().isdigit() and len(value.strip()) >= 9:
        value = int(value.strip())
    if isinstance(value, (int, float)):
        seconds = value / 1000 if abs(value) >= 100_000_000_000 else value
        try:
            moment = datetime.fromtimestamp(seconds, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, list) and 3 <= len(value) <= 7 and all(
        isinstance(part, int) and not isinstance(part, bool) for part in value
    ):
        try:
            parts = list(value[:6])
            if len(parts) == 3:
                return datetime(*parts).date().isoformat()
            return datetime(*parts).isoformat()
        except ValueError:
            return None
    elif isinstance(value, str) and "T" in value:
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return value
        if parsed.tzinfo is None:
            return value
        moment = parsed
    if moment is None:
        return value
    if zone is not None:
        return moment.astimezone(zone).replace(tzinfo=None).isoformat()
    return moment.astimezone(UTC).replace(tzinfo=None).isoformat()


def _safe_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.lower().startswith(_UNSAFE_URL_PREFIXES):
        return None
    return text


def _name_of(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return None
    if isinstance(value, dict):
        for key in _NAME_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None
    if isinstance(value, list) and value:
        return _name_of(value[0])
    return None


def _image(value: Any) -> str | None:
    if isinstance(value, str):
        return _safe_url(value)
    if isinstance(value, dict):
        for key in _IMAGE_DICT_KEYS:
            candidate = _safe_url(value.get(key))
            if candidate:
                return candidate
        return None
    if isinstance(value, list) and value:
        return _image(value[0])
    return None


def _category_names(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        name = _name_of(item) if not isinstance(item, str) else (item.strip() or None)
        if name and name not in names:
            names.append(name)
    return names


def _address(value: Any) -> str | None:
    """A one-line address from a string or a structured address object."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        parts = [
            str(value.get(key)).strip()
            for key in ("street", "streetAddress", "address", "line1", "city", "state", "zip",
                        "postalCode")
            if isinstance(value.get(key), (str, int)) and str(value.get(key)).strip()
        ]
        return ", ".join(parts) or None
    return None


def _scalar_id(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (str, int)) and str(value).strip():
        return str(value).strip()
    return None


class PagedJsonEventsPattern:
    name = NAME

    def extract(self, response: FetchResponse, config: SiteConfiguration) -> list[EventCandidate]:
        try:
            payload = json.loads(response.text)
        except (json.JSONDecodeError, ValueError):
            return []
        root = config.json_paths.get("events_root") or record_root(payload) or EVENTS_ROOT_DEFAULT
        records = records_at(payload, root)
        if not records:
            return []

        # Configured paths win; anything not configured is inferred from this
        # page's own records with the same deterministic rules the proposer uses.
        paths = {
            **infer_field_paths(records),
            **{k: v for k, v in config.json_paths.items() if k in _FIELD_KEYS and v},
        }
        start_path = paths.get("start_datetime")
        instance_list = _instance_list(start_path) if start_path else None

        candidates: list[EventCandidate] = []
        for index, record in enumerate(records):
            instances: list[tuple[int | None, Any]] = [(None, record)]
            if instance_list is not None:
                nested = record.get(instance_list)
                if isinstance(nested, list) and nested:
                    instances = [
                        (i, item)
                        for i, item in enumerate(nested[:MAX_INSTANCES_PER_RECORD])
                        if item not in (None, "", [], {})
                    ]
            for instance_index, instance in instances:
                candidates.append(
                    self._candidate(
                        response, config, root, index, record, paths,
                        instance_list, instance_index, instance,
                    )
                )
        return candidates

    def _candidate(
        self,
        response: FetchResponse,
        config: SiteConfiguration,
        root: str,
        index: int,
        record: dict,
        paths: dict[str, str],
        instance_list: str | None,
        instance_index: int | None,
        instance: Any,
    ) -> EventCandidate:
        raw: dict[str, Any] = {}
        field_source_paths: dict[str, str] = {}
        for field_name, path in paths.items():
            source: Any = record
            effective = path
            if (
                instance_list is not None
                and instance_index is not None
                and _instance_list(path) == instance_list
            ):
                # Read this field from the current instance, not instance 0.
                source = instance
                effective = path.split(".", 2)[2] if path.count(".") >= 2 else ""
                shown = f"{instance_list}[{instance_index}]" + (
                    f".{effective}" if effective else ""
                )
            else:
                shown = path
            raw[field_name] = (
                resolve_json_path(source, effective).value if effective else source
            )
            field_source_paths[field_name] = f"{NAME}.{root}[{index}].{shown}"

        record_id = _scalar_id(raw.get("external_source_id"))
        if record_id is not None and instance_index is not None and instance_index > 0:
            record_id = f"{record_id}#{instance_index}"
        raw["external_source_id"] = record_id

        if not isinstance(raw.get("title"), str):
            raw["title"] = _name_of(raw.get("title"))
        raw["start_datetime"] = local_datetime_string(raw.get("start_datetime"), config.timezone)
        raw["end_datetime"] = local_datetime_string(raw.get("end_datetime"), config.timezone)
        url = _safe_url(raw.get("canonical_url"))
        raw["canonical_url"] = url if url and _looks_like_url_value(url) else None
        raw["venue"] = _name_of(raw.get("venue"))
        raw["address"] = _address(raw.get("address"))
        raw["image"] = _image(raw.get("image"))
        names = _category_names(raw.get("categories"))
        raw["categories"] = names or None
        raw["source_category"] = names[0] if names else None
        if isinstance(raw.get("description"), (dict, list)):
            raw["description"] = None

        raw_record_hash = hashlib.sha256(
            json.dumps(
                {"record": record, "instance": instance_index}, sort_keys=True, default=str
            ).encode("utf-8")
        ).hexdigest()
        return EventCandidate(
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


def _looks_like_url_value(value: str) -> bool:
    return value.lower().startswith(("http://", "https://", "/"))
