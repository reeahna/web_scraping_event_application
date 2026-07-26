"""Finding the event array inside an arbitrary JSON document, deterministically.

Shared by the JSON-in-script detectors and proposers (embedded_json,
next_data, nuxt_payload). It does not *guess* which array is the events — it
*scores* every array of objects by how many of its items look like an event
(an event has a title-like key and a date-like key) and only reports an array
that clears a threshold. An unrelated array of, say, navigation links scores
near zero and is never chosen.

Bounded: it walks at most `MAX_NODES` nodes to a depth of `MAX_DEPTH`, so a
pathological document cannot make this expensive.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MAX_DEPTH = 8
MAX_NODES = 5000
MIN_ARRAY_SIZE = 3
MIN_EVENT_LIKE_RATE = 0.6

TITLE_KEYS: frozenset[str] = frozenset(
    {"title", "name", "headline", "summary", "eventname", "event_name"}
)
DATE_KEYS: frozenset[str] = frozenset(
    {
        "start", "startdate", "start_date", "startdatetime", "start_datetime",
        "date", "datetime", "begin", "dtstart", "startsat", "starts_at", "when",
    }
)
URL_KEYS: tuple[str, ...] = ("url", "link", "permalink", "canonical_url", "href", "eventurl")
END_KEYS: tuple[str, ...] = ("end", "enddate", "end_date", "enddatetime", "end_datetime", "dtend")
VENUE_KEYS: tuple[str, ...] = ("venue", "location", "place", "venuename")
ADDRESS_KEYS: tuple[str, ...] = ("address", "streetaddress", "fulladdress")
IMAGE_KEYS: tuple[str, ...] = ("image", "imageurl", "image_url", "photo", "thumbnail")
CATEGORY_KEYS: tuple[str, ...] = ("category", "categories", "type", "genre", "tags")
ID_KEYS: tuple[str, ...] = ("id", "uid", "identifier", "guid", "eventid", "event_id")


@dataclass(frozen=True)
class EventArrayCandidate:
    path: str
    size: int
    event_like_rate: float
    sample_keys: tuple[str, ...]

    @property
    def score(self) -> float:
        # Rate dominates; a larger array breaks ties.
        return round(self.event_like_rate + min(self.size, 50) / 1000.0, 4)


def _norm(key: str) -> str:
    return key.replace("-", "").replace("_", "").replace(" ", "").casefold()


def _item_is_event_like(item: dict) -> bool:
    keys = {_norm(k) for k in item}
    has_title = bool(keys & {_norm(k) for k in TITLE_KEYS})
    has_date = bool(keys & {_norm(k) for k in DATE_KEYS})
    return has_title and has_date


def find_event_arrays(document: Any) -> list[EventArrayCandidate]:
    """Every array of objects that looks like an event list, best first."""
    candidates: list[EventArrayCandidate] = []
    budget = [MAX_NODES]

    def walk(node: Any, path: str, depth: int) -> None:
        if depth > MAX_DEPTH or budget[0] <= 0:
            return
        budget[0] -= 1
        if isinstance(node, list):
            objects = [i for i in node if isinstance(i, dict)]
            if len(objects) >= MIN_ARRAY_SIZE:
                like = sum(1 for i in objects if _item_is_event_like(i)) / len(objects)
                if like >= MIN_EVENT_LIKE_RATE:
                    candidates.append(
                        EventArrayCandidate(
                            path=path or "$",
                            size=len(objects),
                            event_like_rate=round(like, 4),
                            sample_keys=tuple(sorted(objects[0].keys()))[:20],
                        )
                    )
            # Recurse into list items only enough to find nested arrays.
            for index, item in enumerate(node[:50]):
                walk(item, f"{path}[{index}]", depth + 1)
        elif isinstance(node, dict):
            for key, value in node.items():
                child = f"{path}.{key}" if path else key
                walk(value, child, depth + 1)

    walk(document, "", 0)
    candidates.sort(key=lambda c: (-c.score, -c.size, c.path))
    return candidates


def _first_key(item: dict, options: tuple[str, ...]) -> str | None:
    lookup = {_norm(k): k for k in item}
    for option in options:
        actual = lookup.get(_norm(option))
        if actual is not None:
            return actual
    return None


def infer_field_paths(sample: dict) -> dict[str, str]:
    """Maps the raw field names the JSON patterns use to the actual keys of a
    sample event object, so a proposal points at real keys, not assumed ones."""
    title = _first_key(sample, tuple(TITLE_KEYS))
    date = _first_key(sample, tuple(DATE_KEYS))
    paths: dict[str, str] = {}
    if title:
        paths["title"] = title
    if date:
        paths["start_datetime"] = date
    for field, options in (
        ("canonical_url", URL_KEYS),
        ("end_datetime", END_KEYS),
        ("venue", VENUE_KEYS),
        ("address", ADDRESS_KEYS),
        ("image", IMAGE_KEYS),
        ("source_category", CATEGORY_KEYS),
        ("external_source_id", ID_KEYS),
    ):
        actual = _first_key(sample, options)
        if actual:
            paths[field] = actual
    return paths
