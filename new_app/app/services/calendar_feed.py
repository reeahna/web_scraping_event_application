"""A town's upcoming events as an iCalendar (.ics) feed.

One request returns a whole town's events in a standard format that calendar
apps can subscribe to and that AI tools and other programs can read without
parsing the HTML. It holds exactly what the public listing shows, through the
same visibility rules.
"""

from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from icalendar import Calendar
from icalendar import Event as CalendarEvent

from app.config import get_settings
from app.models.city import City
from app.models.event import Event
from app.services.seo import absolute_url

# Calendar apps re-fetch a subscribed feed on their own schedule; this asks for
# about four times a day, which is as often as sources are re-imported.
REFRESH_INTERVAL = timedelta(hours=6)
_MAX_DESCRIPTION = 1000


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _event_component(event: Event, *, host: str, tz: ZoneInfo, now: datetime) -> CalendarEvent:
    page = absolute_url(f"/events/{event.id}")
    item = CalendarEvent()
    item.add("uid", f"event-{event.id}@{host}")
    item.add("dtstamp", now)
    item.add("summary", event.title)
    item.add("url", page)

    if event.start_time is not None:
        item.add("dtstart", datetime.combine(event.start_date, event.start_time, tzinfo=tz))
        if event.end_time is not None:
            end = datetime.combine(event.end_date or event.start_date, event.end_time, tzinfo=tz)
            if end > item.decoded("dtstart"):
                item.add("dtend", end)
    else:
        # An all-day event; iCalendar's end date is exclusive.
        item.add("dtstart", event.start_date)
        if event.end_date is not None and event.end_date > event.start_date:
            item.add("dtend", event.end_date + timedelta(days=1))

    place = ", ".join(p for p in (event.public_venue, event.public_address) if p)
    if place:
        item.add("location", place)
    if event.public_latitude is not None and event.public_longitude is not None:
        item.add("geo", (event.public_latitude, event.public_longitude))
    category = event.effective_category
    if category:
        item.add("categories", [category.name])
    if event.is_cancelled:
        item.add("status", "CANCELLED")

    description = (event.description or "").strip()
    if len(description) > _MAX_DESCRIPTION:
        description = description[:_MAX_DESCRIPTION].rstrip() + "…"
    details = f"Details: {page}"
    if event.canonical_url:
        details += f"\nFrom {event.source}: {event.canonical_url}"
    item.add("description", f"{description}\n\n{details}" if description else details)
    return item


def town_calendar(city: City, events: list[Event]) -> bytes:
    settings = get_settings()
    host = urlsplit(settings.public_base_url).hostname or "localhost"
    now = datetime.now(UTC)

    calendar = Calendar()
    calendar.add("prodid", f"-//{settings.app_name}//Town events//EN")
    calendar.add("version", "2.0")
    calendar.add("x-wr-calname", f"{settings.app_name}: {city.name}")
    calendar.add("x-wr-caldesc", f"Upcoming events in {city.name}, from {absolute_url('/')}")
    calendar.add("x-wr-timezone", city.timezone)
    calendar.add("refresh-interval", REFRESH_INTERVAL, parameters={"VALUE": "DURATION"})
    # An X- property is written as given, so it gets the ISO 8601 form itself.
    calendar.add("x-published-ttl", "PT6H")

    for event in events:
        if event.start_date is None:
            continue
        tz = _zone(event.timezone or city.timezone)
        calendar.add_component(_event_component(event, host=host, tz=tz, now=now))
    # Times carry their zone (TZID); this adds the matching VTIMEZONE blocks
    # strict calendar apps need to place them correctly.
    calendar.add_missing_timezones()
    return calendar.to_ical()
