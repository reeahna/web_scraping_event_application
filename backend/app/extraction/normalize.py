"""EventNormalizer: deterministic conversion from raw extracted values to
typed, cleaned fields.

Never invents a missing title, date, time, venue, address, coordinates,
category, or canonical URL — only applies configured fallback behavior
(e.g. `allow_page_url_as_canonical_fallback`, already resolved by the
pattern before this runs). No LLM call, no geocoding, anywhere in this
module or this engine.
"""

from __future__ import annotations

import dataclasses
import html
import re
from datetime import UTC, date, datetime, time
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from app.core.timezones import is_unset_zone, resolve_zone_name
from app.extraction.date_ranges import parse_date_range
from app.extraction.locations import has_venue
from app.extraction.sanitize import strip_to_text
from app.extraction.transform import apply_transformations, parse_date_value, parse_time_value
from app.extraction.types import EventCandidate
from app.schemas.extraction import SiteConfiguration, TransformationRuleConfig

# In transformation_history when the source marks the event as all-day.
ALL_DAY_MARKER = "all_day"
# The source flags the event all-day, but the site's configuration says a
# date with no time means "time not announced" (see
# SiteConfiguration.date_only_means_time_tbd). The event reads "Time TBD".
TIME_TBD_MARKER = "all_day_flag_means_time_tbd"
# In transformation_history when the source states the event has no end (a
# feed field present and empty), as opposed to the end simply not being read.
END_ABSENT_MARKER = "end:absent_in_source"
# In transformation_history when the venue (and address) came from the
# source's administrator-set home venue rather than from the event itself.
HOME_VENUE_MARKER = "venue:source_home_venue"
HOME_VENUE_SOURCE_PATH = "source:home_venue"

_ISO_DATETIME_SPLIT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(.*)$")


def _clean_whitespace(value: object) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


# Raw fields that can supply an event's description, best first. A source's
# own short summary/excerpt stands in when it has no full description.
DESCRIPTION_FIELDS = ("description", "summary", "excerpt")


def _has_text(value: object) -> bool:
    return value.strip() != "" if isinstance(value, str) else value is not None


def plain_text(value: object) -> str | None:
    """Whitespace-cleaned text with HTML entities decoded. Feeds hand titles
    over as "Rock &amp; Roll" (some twice: "&amp;amp;"), and the template
    escapes again, so the page showed the entity instead of "&"."""
    if value is None:
        return None
    text = str(value)
    for _ in range(2):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return _clean_whitespace(text)


def _rules_for_field(
    rules: list[TransformationRuleConfig], field_name: str
) -> list[TransformationRuleConfig]:
    return [r for r in rules if r.field == field_name]


_NAIVE_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$")
_UTC_PATH_SUFFIXES = ("_gmt", "_utc")


def _mark_utc(value: object, source_path: str | None) -> object:
    """A naive date-time read from a field the source names as UTC
    (WordPress `date_gmt`, LiveWhale `date_utc`) as an explicit UTC instant,
    so it is moved to the event's own clock instead of being read as local
    time (8 PM Eastern stored as "1 AM the next day")."""
    if not isinstance(value, str) or not source_path:
        return value
    key = source_path.rsplit(".", 1)[-1].split("[", 1)[0].lower()
    text = value.strip()
    if key.endswith(_UTC_PATH_SUFFIXES) and _NAIVE_DATETIME_RE.match(text):
        return text.replace(" ", "T", 1) + "+00:00"
    return value


_ISO_OFFSET_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}.*(Z|[+-]\d{2}:?\d{2})$")


def _to_local(value: object, zone: ZoneInfo | None) -> object:
    """An instant that carries its own UTC offset ("2026-11-20T01:00:00Z",
    "...-05:00", or an aware datetime) as the event's local wall-clock
    datetime, so an 8 PM show in Bloomington stays at 8 PM on its own day
    instead of becoming 1 AM the next day. Values without an offset (local
    times, date-only values, prose) are returned unchanged: a date-only value
    is a calendar date, never a UTC midnight. Without a known zone the value is
    left as the source wrote it (its own offset's wall clock)."""
    if zone is None:
        return value
    moment: datetime | None = None
    if isinstance(value, datetime):
        moment = value if value.tzinfo is not None else None
    elif isinstance(value, str) and _ISO_OFFSET_RE.match(value.strip()):
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            moment = None
    if moment is None or moment.tzinfo is None:
        return value
    return moment.astimezone(zone).replace(tzinfo=None)


def _localize_time(
    day: date | None, moment: time | None, zone: ZoneInfo | None
) -> tuple[date | None, time | None]:
    """A separately-given time with an offset ("19:00:00-05:00") moved to the
    event's zone, together with its date (which can change at midnight)."""
    if day is None or moment is None or moment.tzinfo is None or zone is None:
        return day, moment
    local = datetime.combine(day, moment).astimezone(zone)
    return local.date(), local.time().replace(tzinfo=None)


_ALL_DAY_TEXT_RE = re.compile(r"^\s*all[\s-]?day\s*$", re.IGNORECASE)
_DAY_END_TIMES = (time(23, 59), time(23, 59, 59))


def _says_all_day(
    flag: object,
    start_time: time | None,
    end_time: time | None,
    start_date: date | None,
    end_date: date | None,
) -> bool:
    """True when the source itself says the event lasts the whole day.

    A boolean flag (iCalendar VALUE=DATE, a platform's `is_all_day`) is the
    source's word and is taken as given. A text value ("All day", the display
    string some library calendars send next to 00:00 and 23:59) counts only
    when the clock times agree with it: no time, or a start at midnight that
    ends at midnight or 11:59 PM on the same day. A time range on its own is
    never enough, so a real 12:00 AM to 11:59 PM listing with no all-day
    wording, a midnight start, or a 10 PM to 2 AM event keeps its times."""
    if flag is True:
        return True
    if not isinstance(flag, str) or not _ALL_DAY_TEXT_RE.match(flag):
        return False
    if start_time is None:
        return end_time is None
    if start_time != time(0):
        return False
    if end_date is not None and start_date is not None and end_date != start_date:
        return False
    return end_time is None or end_time == time(0) or end_time in _DAY_END_TIMES


def _split_iso_datetime(value: object) -> tuple[object | None, str | None]:
    """Only splits on "T" when it's the actual ISO 8601 date/time separator
    (immediately after a YYYY-MM-DD date) — a naive `"T" in value` substring
    check would also match free-text weekday names like "Tuesday"/"Thursday"
    (as generic_html_cards sources often emit) and silently discard the date
    by partitioning inside the word itself.

    A `parse_date`/`parse_time` transformation may already have produced a
    `date`/`datetime` object; those are passed straight through to the date
    parser (which accepts them as-is) rather than string-split."""
    if value is None:
        return None, None
    if isinstance(value, datetime):
        return value.date(), value.time().isoformat()
    if isinstance(value, date):
        return value, None
    if not isinstance(value, str) or not value:
        return (value or None), None
    match = _ISO_DATETIME_SPLIT_RE.match(value)
    if match:
        date_part, time_part = match.groups()
        return date_part or None, time_part or None
    return value, None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def local_today(zone_name: str | None) -> date:
    """Today's date in `zone_name` (an IANA zone). A town still on the UTC
    default, or an unknown zone, uses the UTC date."""
    now = _utc_now()
    if zone_name and not is_unset_zone(zone_name):
        try:
            return now.astimezone(ZoneInfo(zone_name)).date()
        except (KeyError, ValueError):
            pass
    return now.date()


def normalize_candidate(
    candidate: EventCandidate,
    config: SiteConfiguration,
    *,
    fallback_timezone: str | None = None,
) -> EventCandidate:
    """Deterministic: identical `candidate.raw` + `config` +
    `fallback_timezone` always produces an identical normalized
    EventCandidate — required for the engine's repeated-run determinism
    guarantee."""
    raw = candidate.raw
    history: list[str] = list(candidate.transformation_history)
    warnings: list[str] = list(candidate.warnings)
    # Year-less dates ("Nov 20") resolve against the town's own date, not the
    # server's UTC date, which is already tomorrow during a US evening.
    town_today = local_today(resolve_zone_name(config.timezone, fallback_timezone))

    def apply(field_name: str, value: object) -> object:
        rules = _rules_for_field(config.transformations, field_name)
        if not rules:
            return value
        try:
            result, applied = apply_transformations(value, rules, reference_date=town_today)
        except ValueError as exc:
            # A rule that cannot run (an unsafe or invalid regex saved before
            # it was checked) leaves the value as extracted and says why,
            # rather than failing the whole preview or import.
            warnings.append(f"transformation_skipped:{field_name}:{exc}")
            return value
        history.extend(applied)
        return result

    title = plain_text(apply("title", raw.get("title")))

    description_raw = apply(
        "description",
        next((raw[f] for f in DESCRIPTION_FIELDS if _has_text(raw.get(f))), None),
    )
    # Templates never render descriptions with a `|safe` filter (confirmed:
    # no such usage exists anywhere in this codebase), so a tag-preserving
    # sanitized value would just display as literal escaped markup. Plain
    # text is what the current rendering path actually needs.
    description = strip_to_text(description_raw) if description_raw is not None else None

    venue = plain_text(apply("venue", raw.get("venue")))
    address = plain_text(apply("address", raw.get("address")))
    source_category = plain_text(apply("source_category", raw.get("source_category")))

    external_source_id = apply("external_source_id", raw.get("external_source_id"))
    if external_source_id is not None:
        external_source_id = str(external_source_id).strip() or None

    canonical_url = apply("canonical_url", raw.get("canonical_url"))
    if canonical_url:
        canonical_url = urljoin(candidate.source_page, str(canonical_url))

    image_url = apply("image", raw.get("image"))
    if image_url:
        image_url = urljoin(candidate.source_page, str(image_url))

    timezone = resolve_zone_name(config.timezone, fallback_timezone)
    # With no real zone (a town still on the UTC default) an offset-carrying
    # time keeps the source's own wall clock rather than being moved to UTC.
    zone = None if is_unset_zone(timezone) else ZoneInfo(timezone)
    paths = candidate.field_source_paths
    start_date_part, start_time_from_dt = _split_iso_datetime(
        _to_local(
            _mark_utc(apply("start_datetime", raw.get("start_datetime")),
                      paths.get("start_datetime")),
            zone,
        )
    )
    end_date_part, end_time_from_dt = _split_iso_datetime(
        _to_local(
            _mark_utc(apply("end_datetime", raw.get("end_datetime")), paths.get("end_datetime")),
            zone,
        )
    )
    start_time_raw = apply("start_time", raw.get("start_time")) or start_time_from_dt
    end_time_raw = apply("end_time", raw.get("end_time")) or end_time_from_dt

    start_date = parse_date_value(start_date_part, config.date_formats)
    end_date = parse_date_value(end_date_part, config.date_formats)
    start_time = parse_time_value(start_time_raw, config.time_formats)
    end_time = parse_time_value(end_time_raw, config.time_formats)

    # Shared date-range handling (Phase 8G). A dedicated `date_range` field is
    # the explicit path; failing that, a start value the plain parser could not
    # read is offered to the range parser, so a column mixing single dates and
    # multi-day ranges works without per-site configuration. The range parser
    # never invents a missing year/month/end — an ambiguous value is recorded
    # as a warning and leaves the dates unset.
    range_input = apply("date_range", raw.get("date_range"))
    had_explicit_range = range_input is not None and str(range_input).strip() != ""
    if not had_explicit_range and start_date is None and start_date_part:
        range_input = start_date_part
    if range_input is not None and str(range_input).strip() != "":
        range_result = parse_date_range(range_input)
        if range_result.matched:
            if start_date is None:
                start_date = range_result.start_date
            if end_date is None and range_result.end_date is not None:
                end_date = range_result.end_date
            history.append(f"date_range:{range_result.form}")
        elif range_result.ambiguous:
            warnings.append(f"date_range_ambiguous:{range_result.reason}")

    if start_date_part is not None and start_date is None:
        warnings.append(f"unparseable_start_date:{start_date_part}")
    if end_date_part is not None and end_date is None:
        warnings.append(f"unparseable_end_date:{end_date_part}")

    # A time that still carries an offset (given apart from its date) is moved
    # to the event's zone with its date. Stored times are always the event's
    # local wall clock; `timezone` names the zone they are in.
    if start_time is not None and start_time.tzinfo is not None:
        if zone is not None:
            start_date, start_time = _localize_time(start_date, start_time, zone)
            history.append("start_time:converted_to_event_timezone")
        start_time = start_time.replace(tzinfo=None) if start_time else None
    if end_time is not None and end_time.tzinfo is not None:
        if zone is not None:
            end_day = end_date or start_date
            converted_day, end_time = _localize_time(end_day, end_time, zone)
            if end_date is not None:
                end_date = converted_day
            history.append("end_time:converted_to_event_timezone")
        end_time = end_time.replace(tzinfo=None) if end_time else None

    says_all_day = start_date is not None and _says_all_day(
        raw.get("all_day"), start_time, end_time, start_date, end_date
    )
    if says_all_day and config.date_only_means_time_tbd and raw.get("all_day") is True:
        # Only the bare flag (a date with no time) is reread as "time not
        # announced". Explicit "All day" wording from the source still counts.
        start_time = end_time = None
        history.append(TIME_TBD_MARKER)
    elif says_all_day:
        # An all-day event has no clock time; a re-scrape clears a stale one
        # (see app.repositories.event.update_event).
        start_time = end_time = None
        history.append(ALL_DAY_MARKER)
    if raw.get("end_absent") is True and end_date is None and end_time is None:
        history.append(END_ABSENT_MARKER)

    if config.home_venue_name and not has_venue({"venue": venue}):
        # A source-level default, used only when the event names no venue.
        # Its address goes with it only when the event has none of its own.
        venue = config.home_venue_name
        paths = {**paths, "venue": HOME_VENUE_SOURCE_PATH}
        if not address and config.home_venue_address:
            address = config.home_venue_address
            paths["address"] = HOME_VENUE_SOURCE_PATH
        history.append(HOME_VENUE_MARKER)

    latitude = _parse_coordinate(raw.get("latitude"), warnings, "latitude")
    longitude = _parse_coordinate(raw.get("longitude"), warnings, "longitude")

    return dataclasses.replace(
        candidate,
        title=_fit_text(title, TITLE_MAX, warnings, "title"),
        canonical_url=_without_nul(canonical_url),
        description=_without_nul(description),
        start_date=start_date,
        start_time=start_time,
        end_date=end_date,
        end_time=end_time,
        timezone=timezone,
        venue=_fit_text(venue, VENUE_MAX, warnings, "venue"),
        address=_fit_text(address, ADDRESS_MAX, warnings, "address"),
        image_url=_fit_url(image_url, URL_MAX, warnings),
        latitude=latitude,
        longitude=longitude,
        source_category=_fit_text(source_category, SOURCE_CATEGORY_MAX, warnings,
                                  "source category"),
        external_source_id=_without_nul(external_source_id),
        field_source_paths=paths,
        transformation_history=tuple(history),
        warnings=tuple(warnings),
    )


# The longest text each stored event column holds (app.models.event.Event).
# PostgreSQL refuses a longer value, and one such event used to fail the whole
# import; SQLite stored it regardless. An over-long canonical_url or source ID
# is left as it is: shortening it would change the event's identity, and the
# validator rejects that one event instead.
TITLE_MAX = 500
VENUE_MAX = 500
ADDRESS_MAX = 1000
SOURCE_CATEGORY_MAX = 255
URL_MAX = 2000
EXTERNAL_ID_MAX = 255


def _without_nul(value: str | None) -> str | None:
    """A NUL byte is never meaningful in event text, and PostgreSQL text
    columns cannot hold one."""
    if value is None:
        return None
    return value.replace("\x00", "") or None


def _fit_text(value: str | None, limit: int, warnings: list[str], label: str) -> str | None:
    value = _without_nul(value)
    if value is None or len(value) <= limit:
        return value
    warnings.append(f"{label} was {len(value)} characters; shortened to {limit}")
    return value[: limit - 3].rstrip() + "..."


def _fit_url(value: str | None, limit: int, warnings: list[str]) -> str | None:
    """An image link cut short would point nowhere, so an over-long one is dropped."""
    value = _without_nul(value)
    if value is None or len(value) <= limit:
        return value
    warnings.append(f"image link was {len(value)} characters; left out")
    return None


def _parse_coordinate(value: object, warnings: list[str], label: str) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        warnings.append(f"unparseable_{label}:{value}")
        return None
