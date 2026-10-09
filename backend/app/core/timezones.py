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
"""

from __future__ import annotations

from datetime import datetime
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
