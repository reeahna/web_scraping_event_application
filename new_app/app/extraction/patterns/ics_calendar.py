"""iCalendar (ICS) extraction pattern.

Parses a VCALENDAR feed with the maintained `icalendar` library — never a
hand-rolled line scanner — so folded lines, escaped values, parameters and
timezones are handled correctly. Fetches `config.api_endpoint` or
`config.listing_url`; the feed's content type (`text/calendar`) is allowed via
the site configuration's `fetch.allowed_content_types`, which the proposer
sets.

Field handling:

* `UID` is the stable identity, mapped to `external_source_id` — ICS events
  frequently have no public URL, and this pattern never fabricates one. The
  proposer therefore does not require `canonical_url`; dedup uses the UID.
* `DTSTART`/`DTEND` produce a date for an all-day (`VALUE=DATE`) event and a
  naive wall-clock datetime otherwise, in the same ISO shape every other
  pattern emits — this engine performs no timezone math (see
  app.extraction.normalize), so a `TZID` only labels the value.
* `STATUS:CANCELLED` events are extracted but flagged; they are never silently
  dropped, and (per the recurrence rules that arrive in Phase 8G) never used
  to auto-delete anything.
* `RRULE`/`RDATE`/`EXDATE`/`RECURRENCE-ID` are preserved verbatim in `raw` for
  the Phase 8G recurrence expander; this pattern emits one candidate per
  VEVENT (the parent), never a guessed expansion.
"""

from __future__ import annotations

import hashlib
from typing import Any

from icalendar import Calendar

from app.extraction.types import EventCandidate, FetchResponse
from app.schemas.extraction import SiteConfiguration

NAME = "ics_calendar"
PATTERN_VERSION = "1"


def _to_iso(value: Any) -> tuple[str | None, bool]:
    """(iso_string, all_day). `value` is an icalendar vDDDTypes wrapping a
    date or datetime."""
    if value is None:
        return None, False
    dt = getattr(value, "dt", value)
    # A bare date (all-day) has no time component.
    if hasattr(dt, "hour"):
        return dt.strftime("%Y-%m-%dT%H:%M:%S"), False
    if hasattr(dt, "isoformat"):
        return dt.isoformat(), True
    return str(dt), False


def _text(component, key: str) -> str | None:
    value = component.get(key)
    if value is None:
        return None
    return str(value).strip() or None


def _categories(component) -> str | None:
    raw = component.get("categories")
    if raw is None:
        return None
    # icalendar returns a vCategory (or list) whose cats are vText.
    cats = getattr(raw, "cats", None)
    if cats:
        return ", ".join(str(c) for c in cats) or None
    return str(raw).strip() or None


class IcsCalendarPattern:
    name = NAME

    def extract(self, response: FetchResponse, config: SiteConfiguration) -> list[EventCandidate]:
        try:
            calendar = Calendar.from_ical(response.text)
        except (ValueError, Exception):  # noqa: BLE001 - malformed feed must not crash
            return []

        candidates: list[EventCandidate] = []
        for index, component in enumerate(calendar.walk("VEVENT")):
            uid = _text(component, "uid")
            start_iso, start_all_day = _to_iso(component.get("dtstart"))
            end_iso, _ = _to_iso(component.get("dtend"))
            status = (_text(component, "status") or "").upper()

            raw: dict[str, Any] = {
                "external_source_id": uid,
                "title": _text(component, "summary"),
                "description": _text(component, "description"),
                "canonical_url": _text(component, "url"),
                "start_datetime": start_iso,
                "end_datetime": end_iso,
                "venue": _text(component, "location"),
                "source_category": _categories(component),
                # Preserved for the Phase 8G recurrence expander; never parsed
                # or expanded here.
                "recurrence": {
                    "rrule": _text(component, "rrule"),
                    "rdate": _text(component, "rdate"),
                    "exdate": _text(component, "exdate"),
                    "recurrence_id": _text(component, "recurrence-id"),
                },
                "all_day": start_all_day,
            }
            field_source_paths = {
                key: f"vevent[{index}].{key}"
                for key in ("external_source_id", "title", "start_datetime", "canonical_url")
            }
            warnings: list[str] = []
            if status == "CANCELLED":
                warnings.append("ics_event_cancelled")

            raw_record_hash = hashlib.sha256(
                (uid or component.to_ical().decode("utf-8", "replace")).encode("utf-8")
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
                    warnings=tuple(warnings),
                    raw_record_hash=raw_record_hash,
                )
            )
        return candidates
