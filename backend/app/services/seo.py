"""Search-engine and social metadata for the public site.

Kept out of the templates because every value here has to be an absolute URL
built from configuration: a request's Host header is attacker-controlled, so
deriving a canonical link or an Open Graph URL from it would let a visitor
decide what address search engines and social previews record.
"""

from __future__ import annotations

import json
import math
import time as time_module
from datetime import date, datetime, time
from urllib.parse import quote, urlencode, urljoin
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.core.formatting import human_date_long
from app.core.timezones import is_unset_zone, resolve_zone_name
from app.core.us_states import is_us_state, state_name
from app.models.event import Event

# A town page with fewer upcoming events than this is kept out of search
# results and the sitemap. An empty "0 events" page is what search engines call
# thin content, and enough of them lowers how the whole site is ranked. The
# page still works for visitors and joins the index once its sources fill in.
MIN_INDEXABLE_TOWN_EVENTS = 5


def town_is_indexable(upcoming_events: int) -> bool:
    return upcoming_events >= MIN_INDEXABLE_TOWN_EVENTS


def absolute_url(path: str) -> str:
    """Join a site-relative path onto the configured public base URL."""
    base = get_settings().public_base_url.rstrip("/") + "/"
    return urljoin(base, path.lstrip("/"))


def _combine(day: date | None, clock: time | None, zone: ZoneInfo | None = None) -> str | None:
    """schema.org wants an ISO 8601 date, with a time when one is known.

    A timed value carries its UTC offset when the event's zone is known
    ("2026-12-06T14:00:00-05:00"), worked out for that date so daylight saving
    is right. Without a known zone it is left as a bare local time, which
    Google reads in the timezone of the event's location: guessing an offset
    would be worse than none."""
    if day is None:
        return None
    if clock is None:
        return day.isoformat()
    if zone is not None:
        return datetime.combine(day, clock, tzinfo=zone).isoformat()
    return f"{day.isoformat()}T{clock.isoformat()}"


def _event_zone(event: Event) -> ZoneInfo | None:
    """The zone the event's wall-clock times are in: its own, else its
    town's. None when neither is set (a town left on the UTC default)."""
    own = event.timezone
    town = event.city.timezone if event.city is not None else None
    for candidate in (own, town):
        if is_unset_zone(candidate):
            continue
        resolved = resolve_zone_name(candidate)
        if resolved:
            return ZoneInfo(resolved)
    return None


def script_safe_json(data: dict) -> str:
    """JSON for a raw <script type="application/ld+json"> block. Escapes the
    characters that could close that element or open another: a scraped title
    containing "</script><img ...>" would otherwise end the block and inject
    markup. \u003c etc. are the same characters to any JSON reader."""
    return (
        json.dumps(data, ensure_ascii=True)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def event_structured_data(event: Event) -> str:
    """A schema.org/Event JSON-LD document for one public event."""
    return script_safe_json({"@context": "https://schema.org", **event_schema(event)})


def event_schema(event: Event) -> dict:
    """The schema.org/Event object for one public event.

    Only fields the public page already shows are included: this describes the
    page, it does not publish anything the visitor could not otherwise read.
    """
    data: dict = {
        "@type": "Event",
        "name": event.title,
        "url": absolute_url(f"/events/{event.id}"),
        "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
        "eventStatus": (
            "https://schema.org/EventCancelled"
            if event.is_cancelled
            else "https://schema.org/EventScheduled"
        ),
    }

    zone = _event_zone(event)
    # An all-day event is a calendar date (or dates), never 00:00 to 23:59 at
    # an offset: that would tell Google it runs around the clock.
    start_clock = None if event.all_day else event.start_time
    end_clock = None if event.all_day else event.end_time
    start = _combine(event.start_date, start_clock, zone)
    if start:
        data["startDate"] = start
    # Only when an end is actually recorded. Falling back to the start date
    # emitted a date-only endDate for a timed event, which reads as "ends at
    # midnight" rather than "end unknown".
    if event.end_date is not None or end_clock is not None:
        end = _combine(event.end_date or event.start_date, end_clock, zone)
        if end and end != start:
            data["endDate"] = end

    if event.description:
        data["description"] = event.description
    if event.image_url:
        data["image"] = event.image_url

    data["location"] = _event_place(event)
    # No "organizer": the only name on record is the source the event was
    # gathered from (a campus calendar, "- TerpLink"), which is often not who
    # is hosting it. Google only recommends the field, and a wrong host is
    # worse than none.

    if event.canonical_url:
        data["sameAs"] = event.canonical_url

    return data


def event_datetimes(event: Event) -> dict[str, str | None]:
    """ISO values for the event page's <time datetime> attributes, the same
    ones the structured data uses."""
    zone = _event_zone(event)
    end_day = event.end_date or event.start_date
    start_clock = None if event.all_day else event.start_time
    end_clock = None if event.all_day else event.end_time
    return {
        "start_date": event.start_date.isoformat() if event.start_date else None,
        "end_date": event.end_date.isoformat() if event.end_date else None,
        "start": _combine(event.start_date, start_clock, zone),
        "end": _combine(end_day, end_clock, zone) if end_clock else None,
    }


def _event_place(event: Event) -> dict:
    """The event's schema.org Place. Google requires a location with an
    address on every event. When the source names a venue it is used, with
    its street address when one is known; when it names none, the place is
    the event's town, which Google's guidelines ask for ("the city name or
    the most representative location"). A venue is never made up."""
    city = event.city
    venue = event.public_venue
    street = event.public_address

    address: dict = {"@type": "PostalAddress"}
    if street:
        address["streetAddress"] = street
    if city is not None:
        address["addressLocality"] = city.name
        region = state_name(city.state_or_region)
        if region:
            address["addressRegion"] = region
        if city.country:
            address["addressCountry"] = city.country
        elif is_us_state(city.state_or_region):
            address["addressCountry"] = "US"

    place: dict = {"@type": "Place"}
    name = venue or street or (town_place(city) if city is not None else None)
    if name:
        place["name"] = name
    if len(address) > 1:
        place["address"] = address
    latitude, longitude = event.public_latitude, event.public_longitude
    if latitude is not None and longitude is not None:
        place["geo"] = {"@type": "GeoCoordinates", "latitude": latitude, "longitude": longitude}
    return place


def event_page_title(event: Event) -> str:
    """The event's <title>: its name, then the town and day when known, so a
    search result says where and when before anyone clicks.
    "Jazz Night: Ann Arbor, Aug 18". The site name is added by the template."""
    where_when = []
    if event.city is not None:
        where_when.append(event.city.name)
    if event.start_date is not None:
        day = event.start_date
        # By hand rather than strftime("%-d"), which raises on Windows.
        where_when.append(f"{day.strftime('%b')} {day.day}")
    title = " ".join(event.title.split())
    return f"{title}: {', '.join(where_when)}" if where_when else title


def event_meta_description(event: Event) -> str:
    """A one-line summary for search results, capped at a readable length."""
    parts = [event.title]
    venue = event.corrected_venue or event.venue
    if venue:
        parts.append(f"at {venue}")
    if event.city:
        parts.append(f"in {event.city.name}")
    if event.start_date is not None:
        # Built by hand rather than with strftime("%-d"): that flag is a glibc
        # extension and raises on Windows.
        day = event.start_date
        parts.append(f"on {day.strftime('%B')} {day.day}, {day.year}")
    summary = " ".join(parts)
    if event.description:
        summary = f"{summary}. {event.description}"
    summary = " ".join(summary.split())
    return summary[:157].rstrip() + "…" if len(summary) > 158 else summary


# --- town pages and "things to do" searches ------------------------------------
#
# People rarely search for "Bulletin". They search for "things to do in Ann
# Arbor", "events in Ann Arbor this weekend", or "events near me" (which Google
# answers with pages about the town it thinks they are in). Each town therefore
# has three landing pages, each named after the search it answers: all upcoming
# events, this weekend, and today. The windows are real paths rather than
# ?preset= query strings, because a query string variant is canonicalised to
# the town page and so can never rank on its own.

# Path segment -> (filter preset, words used in titles and headings).
TOWN_WINDOWS: dict[str, tuple[str, str]] = {
    "this-weekend": ("weekend", "this weekend"),
    "today": ("today", "today"),
}

# How far away another college town can be and still be listed as nearby.
NEARBY_TOWN_MAX_MILES = 250
NEARBY_TOWN_LIMIT = 4


def town_place(city) -> str:
    """ "Ann Arbor, Michigan", or just the name when no state is recorded.
    The state is always spelled out, whether the town was entered as "MI" or
    "Michigan", so every town's title and heading reads the same way."""
    state = state_name(city.state_or_region)
    return f"{city.name}, {state}" if state else city.name


def town_path(city, window: str | None = None) -> str:
    return f"/city/{city.slug}/{window}" if window else f"/city/{city.slug}"


def town_heading(city, window: str | None = None) -> str:
    """The page's <h1>, worded the way people search for it."""
    if window:
        return f"Things to do in {city.name} {TOWN_WINDOWS[window][1]}"
    return f"Things to do in {city.name}"


def town_page_title(city, window: str | None = None) -> str:
    """The town page's <title>, without the site name (the template adds it).

    Leads with the town and its state so the result matches both "events in
    Ann Arbor" and "things to do in Ann Arbor, MI", and so a reader in another
    Bloomington can tell which one it is before clicking."""
    place = town_place(city)
    if window:
        return f"Things to do in {place} {TOWN_WINDOWS[window][1]}"
    return f"Events in {place}: things to do this week"


# A search result shows about 155 characters of a description; what runs past
# that is cut off mid-word. Big towns list up to eight schools, so only the
# first two are named.
META_DESCRIPTION_LIMIT = 158
DESCRIPTION_SCHOOLS = 2


def town_meta_description(
    city, *, upcoming: int, window: str | None = None, window_count: int | None = None
) -> str:
    place = town_place(city)
    if window:
        when = TOWN_WINDOWS[window][1]
        count = window_count or 0
        lead = (
            f"{count} event{'s' if count != 1 else ''} in {place} {when}"
            if count
            else f"What's on in {place} {when}"
        )
    else:
        lead = (
            f"{upcoming} upcoming event{'s' if upcoming != 1 else ''} in {place}"
            if upcoming
            else f"What's on in {place}"
        )
    schools = [u.name for u in city.universities][:DESCRIPTION_SCHOOLS]
    tail = ", updated daily from local calendars."
    if schools:
        full = f"{lead}: concerts, theater, talks and campus events around {' and '.join(schools)}"
        if len(full + tail) <= META_DESCRIPTION_LIMIT:
            return full + tail
        if len(full) + 1 <= META_DESCRIPTION_LIMIT:
            return full + "."
        # One long school name ("University of California, Los Angeles and ...").
        short = f"{lead}: concerts, theater, talks and campus events around {schools[0]}"
        if len(short) + 1 <= META_DESCRIPTION_LIMIT:
            return short + "."
    return f"{lead}: concerts, theater, talks and campus events{tail}"


def _miles_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance (haversine). Good to well under a mile, which is
    all "nearby town" needs."""
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearby_towns(
    city, candidates, *, limit: int = NEARBY_TOWN_LIMIT, centers: dict | None = None
) -> list[tuple]:
    """Other active towns within NEARBY_TOWN_MAX_MILES, nearest first, as
    (city, miles). Links between neighbouring towns help a reader near a town
    line and tell search engines which towns belong to the same area.
    `centers` fills in towns whose admin coordinates are empty (town_centers)."""
    here = town_center(city, centers)
    if here is None:
        return []
    found = []
    for other in candidates:
        if other.id == city.id or not other.is_active:
            continue
        there = town_center(other, centers)
        if there is None:
            continue
        miles = _miles_between(*here, *there)
        if miles <= NEARBY_TOWN_MAX_MILES:
            found.append((other, miles))
    found.sort(key=lambda pair: pair[1])
    return found[:limit]


# --- where a town is ------------------------------------------------------------
#
# A town's position comes from its admin coordinates. No town in production had
# them, so the "Events near" links and the town's geo data never appeared. When
# they are empty, the town is placed at the median of its own public events'
# coordinates: real positions of real venues, never a made-up point. The median
# rather than the mean, so one badly geocoded venue in another state cannot pull
# the town away. Fewer than MIN_CENTER_EVENTS positions is too few to trust.

MIN_CENTER_EVENTS = 3
_CENTER_CACHE_SECONDS = 30 * 60
_center_cache: dict = {"at": 0.0, "key": None, "centers": {}}


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def centers_from_coordinates(rows) -> dict[int, tuple[float, float]]:
    """{city_id: (latitude, longitude)} from (city_id, lat, lon) rows."""
    by_city: dict[int, tuple[list[float], list[float]]] = {}
    for city_id, lat, lon in rows:
        lats, lons = by_city.setdefault(city_id, ([], []))
        lats.append(lat)
        lons.append(lon)
    return {
        city_id: (round(_median(lats), 4), round(_median(lons), 4))
        for city_id, (lats, lons) in by_city.items()
        if len(lats) >= MIN_CENTER_EVENTS
    }


def town_centers(db) -> dict[int, tuple[float, float]]:
    """Every town's position from its events, cached for half an hour: it
    reads every public event's coordinates, and towns do not move."""
    from app.repositories.public_events import local_today, public_event_coordinates

    key = str(db.get_bind().url)
    now = time_module.monotonic()
    cached = _center_cache
    if cached["key"] == key and now - cached["at"] < _CENTER_CACHE_SECONDS:
        return cached["centers"]
    centers = centers_from_coordinates(public_event_coordinates(db, today=local_today(db)))
    _center_cache.update(at=now, key=key, centers=centers)
    return centers


def clear_town_center_cache() -> None:
    _center_cache.update(at=0.0, key=None, centers={})


def town_center(city, centers: dict | None = None) -> tuple[float, float] | None:
    """The town's (latitude, longitude): the admin's values when set, else
    the median of its events' positions, else None."""
    if city.default_latitude is not None and city.default_longitude is not None:
        return (city.default_latitude, city.default_longitude)
    if centers:
        return centers.get(city.id)
    return None


def _city_schema(city, centers: dict | None = None) -> dict:
    data: dict = {"@type": "City", "name": city.name}
    state = state_name(city.state_or_region)
    if state:
        data["containedInPlace"] = {"@type": "State", "name": state}
    center = town_center(city, centers)
    if center is not None:
        data["geo"] = {
            "@type": "GeoCoordinates",
            "latitude": center[0],
            "longitude": center[1],
        }
    return data


def breadcrumb_structured_data(crumbs: list[tuple[str, str]]) -> str:
    """A schema.org BreadcrumbList from (name, site-relative path) pairs. Search
    results show it in place of the bare URL ("Bulletin > Ann Arbor > ..."),
    which says where an event is before anyone clicks."""
    return script_safe_json(
        {
            "@context": "https://schema.org",
            "@type": "BreadcrumbList",
            "itemListElement": [
                {"@type": "ListItem", "position": i, "name": name, "item": absolute_url(path)}
                for i, (name, path) in enumerate(crumbs, start=1)
            ],
        }
    )


def city_listing_structured_data(
    city,
    events: list[Event],
    *,
    page_url: str,
    name: str | None = None,
    centers: dict | None = None,
) -> str:
    """A town page as a schema.org ItemList of the events it shows, so a crawler
    (a search engine or an AI tool) gets every event's details from the one
    listing page without opening each event."""
    schools = [u.name for u in city.universities]
    return script_safe_json(
        {
            "@context": "https://schema.org",
            "@type": "ItemList",
            "name": name or f"Events in {city.name}",
            "url": page_url,
            "about": _city_schema(city, centers),
            **({"keywords": schools} if schools else {}),
            "numberOfItems": len(events),
            "itemListElement": [
                {"@type": "ListItem", "position": i, "item": event_schema(e)}
                for i, e in enumerate(events, start=1)
            ],
        }
    )


def site_structured_data() -> str:
    """The site itself, with its town search, for the home page."""
    settings = get_settings()
    return script_safe_json(
        {
            "@context": "https://schema.org",
            "@type": "WebSite",
            "publisher": {"@id": absolute_url("/") + "#organization"},
            "name": settings.app_name,
            "description": settings.public_tagline,
            "url": absolute_url("/"),
            "potentialAction": {
                "@type": "SearchAction",
                "target": {
                    "@type": "EntryPoint",
                    "urlTemplate": absolute_url("/") + "?q={search_term_string}",
                },
                "query-input": "required name=search_term_string",
            },
        }
    )


def organization_structured_data() -> str:
    """Who runs the site, for the home page: its name, address and logo.
    Bulletin is a website, so there is no street address or opening hours,
    and no LocalBusiness: that would claim a place that does not exist."""
    settings = get_settings()
    return script_safe_json(
        {
            "@context": "https://schema.org",
            "@type": "Organization",
            "@id": absolute_url("/") + "#organization",
            "name": settings.app_name,
            "url": absolute_url("/"),
            "logo": absolute_url("/static/apple-touch-icon.png"),
            "description": (
                "Events and things to do in US college towns, gathered every day "
                "from campus calendars, venues and libraries."
            ),
        }
    )


def event_share_text(event: Event) -> str:
    """One line describing an event for a social post: its title, and the day
    (and town) when known, so the post reads on its own before anyone opens
    the link."""
    parts = [event.title]
    if event.start_date is not None:
        parts.append(human_date_long(event.start_date))
    if event.city is not None:
        parts.append(event.city.name)
    return " · ".join(parts)


def event_share_links(event: Event) -> list[dict[str, str]]:
    """Share links for an event's public page, one per platform.

    Plain links to each platform's own share page, so they work without
    JavaScript and load nothing from those platforms until someone clicks
    (no tracking pixels, no third-party scripts under our CSP). The page URL
    comes from configuration, never from the request, for the same reason as
    the canonical link above.
    """
    url = absolute_url(f"/events/{event.id}")
    text = event_share_text(event)

    def q(params: dict[str, str]) -> str:
        return urlencode(params, quote_via=quote)

    return [
        {"key": "x", "label": "X", "href": "https://x.com/intent/tweet?" + q(
            {"text": text, "url": url})},
        {"key": "facebook", "label": "Facebook",
         "href": "https://www.facebook.com/sharer/sharer.php?" + q({"u": url})},
        {"key": "bluesky", "label": "Bluesky",
         "href": "https://bsky.app/intent/compose?" + q({"text": f"{text} {url}"})},
        {"key": "linkedin", "label": "LinkedIn",
         "href": "https://www.linkedin.com/sharing/share-offsite/?" + q({"url": url})},
        {"key": "reddit", "label": "Reddit",
         "href": "https://www.reddit.com/submit?" + q({"url": url, "title": event.title})},
        {"key": "whatsapp", "label": "WhatsApp",
         "href": "https://wa.me/?" + q({"text": f"{text} {url}"})},
        {"key": "email", "label": "Email",
         "href": "mailto:?" + q({"subject": event.title, "body": f"{text}\n\n{url}"})},
    ]
