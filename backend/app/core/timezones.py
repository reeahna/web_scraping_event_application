"""Timezone validation and daylight-saving-time safety checks.

The application stores IANA timezone names (``America/Indiana/Indianapolis``,
``UTC``) everywhere a timezone is configured. Fixed-offset abbreviations such
as ``EST`` are *technically* recognised by :mod:`zoneinfo`, but they encode a
constant UTC offset that ignores daylight saving time — so using one for a
location that observes DST silently shifts every summer event by an hour.

Nothing here is site-specific: the DST warning is derived purely from the
timezone name and its own offset behaviour, never from a hostname, city, or
institution. It never rewrites a stored value; it only reports so an
administrator can decide.

For date arithmetic (converting a source's UTC instant to the town's wall
clock, or working out what "today" is in a town), resolve_zone_name() reads a
bare US abbreviation such as ``EST`` as the region it stands for, so daylight
saving time is still applied while the stored setting waits for an admin.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, available_timezones

_VALID_TIMEZONES = frozenset(available_timezones())

# Bare offset aliases that legitimately never observe DST. A warning for these
# would be noise, so they are exempt even though their offset is constant.
_LEGITIMATE_FIXED = frozenset(
    {"UTC", "Universal", "Zulu", "GMT", "GMT0", "GMT-0", "GMT+0", "Greenwich", "UCT"}
)

# Two dates on opposite sides of the northern-hemisphere DST boundary. Any
# DST-observing zone reports different UTC offsets on these two instants; a
# fixed-offset abbreviation reports the same one on both.
_WINTER = datetime(2024, 1, 15, 12, 0)
_SUMMER = datetime(2024, 7, 15, 12, 0)


def is_valid_timezone(name: str) -> bool:
    """True if ``name`` is a recognised IANA/zoneinfo timezone name."""
    return name in _VALID_TIMEZONES


def observes_dst(name: str) -> bool:
    """True if the zone's UTC offset changes across the year (i.e. it observes
    daylight saving time). False for fixed-offset zones and unknown names."""
    if name not in _VALID_TIMEZONES:
        return False
    zone = ZoneInfo(name)
    return _WINTER.replace(tzinfo=zone).utcoffset() != _SUMMER.replace(tzinfo=zone).utcoffset()


def is_fixed_offset_abbreviation(name: str) -> bool:
    """True for a bare, DST-less abbreviation like ``EST``/``MST``/``HST``.

    A regional IANA name (``America/New_York``) contains a ``/`` and is never
    flagged even when it happens not to observe DST (e.g. ``America/Phoenix``).
    UTC/GMT aliases are exempt as legitimately fixed.
    """
    if name not in _VALID_TIMEZONES or name in _LEGITIMATE_FIXED:
        return False
    if "/" in name:
        return False
    return not observes_dst(name)


def dst_warning(name: str | None) -> str | None:
    """A human-readable warning when ``name`` is a fixed-offset abbreviation
    that ignores daylight saving time, else None.

    The message is deliberately generic — it recommends an IANA name without
    guessing the source's actual region — because the correct regional zone is
    an administrator decision, not something to infer from the source's
    identity.
    """
    if not name or not is_fixed_offset_abbreviation(name):
        return None
    return (
        f"'{name}' is a fixed-offset abbreviation that ignores daylight saving time. "
        "Prefer a full IANA timezone name (for example "
        "'America/Indiana/Indianapolis' or 'America/New_York') so event times "
        "stay correct across DST transitions."
    )


# What an administrator almost always means by a bare US abbreviation. zoneinfo
# reads "EST" as a constant UTC-5 all year, so a summer 8 PM event converted
# with it lands at 7 PM. These regional zones carry the real DST rules. (Every
# Indiana town Bulletin covers follows the same rules as New York today; a town
# with its own history should still be given its own IANA name.)
_US_ABBREVIATION_ZONES = {
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "EST5EDT": "America/New_York",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "CST6CDT": "America/Chicago",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "MST7MDT": "America/Denver",
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "PST8PDT": "America/Los_Angeles",
    "HST": "Pacific/Honolulu",
}

_DEFAULT_ZONE = "UTC"


def _regional(name: str | None) -> str | None:
    """`name` when it is a usable regional (or genuinely fixed, like UTC) zone."""
    if not name:
        return None
    name = name.strip()
    if name in _US_ABBREVIATION_ZONES:
        return None
    if name not in _VALID_TIMEZONES or is_fixed_offset_abbreviation(name):
        return None
    return name


def resolve_zone_name(name: str | None, fallback: str | None = None) -> str | None:
    """The IANA zone to do date arithmetic in, for a configured `name` and an
    optional `fallback` (a source's own setting falls back to its town's).

    A proper regional name always wins. A bare US abbreviation such as "EST"
    gives way to a regional fallback, and failing that to the region it stands
    for, so daylight saving time is never ignored. None when neither value
    names a zone. Stored values are never rewritten here."""
    for candidate in (name, fallback):
        regional = _regional(candidate)
        if regional and regional not in _LEGITIMATE_FIXED:
            return regional
    for candidate in (name, fallback):
        if candidate and candidate.strip() in _US_ABBREVIATION_ZONES:
            return _US_ABBREVIATION_ZONES[candidate.strip()]
    for candidate in (name, fallback):
        if _regional(candidate):
            return candidate.strip()
    return None


def zone_info(name: str | None, fallback: str | None = None) -> ZoneInfo:
    """ZoneInfo for resolve_zone_name(name, fallback), UTC when neither names
    a zone."""
    return ZoneInfo(resolve_zone_name(name, fallback) or _DEFAULT_ZONE)


def local_date(name: str | None, now: datetime | None = None) -> date:
    """Today's calendar date in that zone (UTC when unknown). `now` must be
    timezone-aware; it defaults to the current instant."""
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(zone_info(name)).date()


def is_unset_zone(name: str | None) -> bool:
    """True for no zone or a UTC alias. A town's timezone defaults to UTC
    until an admin sets it, and no college town runs on UTC, so UTC means
    "not set" when deciding which wall clock an event belongs on."""
    return not name or name.strip() in _LEGITIMATE_FIXED or name.strip() == "Etc/UTC"
