"""Human-readable date/time formatting for public-facing templates.

Registered as Jinja filters (see app.core.templating) so every template
formats dates/times identically instead of duplicating strftime-style logic
inline. Deliberately avoids platform-specific strftime flags (`%-d`/`%#d`
aren't portable between Linux and Windows) by formatting the day-of-month
as a plain int instead.
"""

from datetime import date, time


def human_date(value: date | None) -> str:
    """ "Sat, Aug 1" — compact form for event cards."""
    if value is None:
        return "Date TBD"
    return f"{value.strftime('%a')}, {value.strftime('%b')} {value.day}"


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
