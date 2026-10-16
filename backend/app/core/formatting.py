"""Human-readable date/time formatting for public-facing templates.

Registered as Jinja filters (see app.core.templating) so every template
formats dates/times identically instead of duplicating strftime-style logic
inline. Deliberately avoids platform-specific strftime flags (`%-d`/`%#d`
aren't portable between Linux and Windows) by formatting the day-of-month
as a plain int instead.
"""

from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# A town's timezone defaults to UTC until an admin sets it, and no college town
# runs on UTC, so a UTC zone means "not set" and shows no abbreviation.
_UNSET_ZONES = frozenset({"UTC", "Etc/UTC", "GMT", "Etc/GMT", "UCT", "Universal", "Zulu"})


def human_date(value: date | None) -> str:
    """ "Sat, Aug 1, 2026" — compact form for event cards. The year is included
    so listings spanning more than one year (recurring series, far-future
    events) read unambiguously."""
    if value is None:
        return "Date TBD"
    return f"{value.strftime('%a')}, {value.strftime('%b')} {value.day}, {value.year}"


def human_date_long(value: date | None) -> str:
    """ "Saturday, August 1, 2026" — full form for the event detail page."""
    if value is None:
        return "Date TBD"
    return f"{value.strftime('%A')}, {value.strftime('%B')} {value.day}, {value.year}"


def human_time(value: time | None) -> str:
    """ "6:00 PM" — no leading zero on the hour."""
    if value is None:
        return ""
    hour12 = value.hour % 12 or 12
    period = "AM" if value.hour < 12 else "PM"
    return f"{hour12}:{value.minute:02d} {period}"


def time_zone_abbreviation(zone_name: str | None, on: date | None, at: time | None) -> str:
    """ "EDT", "CST" — the zone's abbreviation on that date, so daylight saving
    time is reflected. Empty when the zone is unknown or not set."""
    if not zone_name or zone_name in _UNSET_ZONES:
        return ""
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        return ""
    moment = datetime.combine(on or date.today(), at or time(12, 0), tzinfo=zone)
    return moment.tzname() or ""


def event_time_zone(event: Any) -> str:
    """The abbreviation for an event's start: the event's own timezone when the
    source gave one, otherwise its town's. Event times are stored as local
    wall-clock times, so this only labels them; it never converts."""
    city = getattr(event, "city", None)
    zone_name = getattr(event, "timezone", None) or (city.timezone if city else None)
    return time_zone_abbreviation(zone_name, event.start_date, event.start_time)
