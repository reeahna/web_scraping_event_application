"""Search-engine and social metadata for the public site.

Kept out of the templates because every value here has to be an absolute URL
built from configuration: a request's Host header is attacker-controlled, so
deriving a canonical link or an Open Graph URL from it would let a visitor
decide what address search engines and social previews record.
"""

from __future__ import annotations

import json
from datetime import date, time
from urllib.parse import urljoin

from app.config import get_settings
from app.models.event import Event


def absolute_url(path: str) -> str:
    """Join a site-relative path onto the configured public base URL."""
    base = get_settings().public_base_url.rstrip("/") + "/"
    return urljoin(base, path.lstrip("/"))


def _combine(day: date | None, clock: time | None) -> str | None:
    """schema.org wants an ISO 8601 date, with a time when one is known."""
    if day is None:
        return None
    return f"{day.isoformat()}T{clock.isoformat()}" if clock else day.isoformat()


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

    start = _combine(event.start_date, event.start_time)
    if start:
        data["startDate"] = start
    # Only when an end is actually recorded. Falling back to the start date
    # emitted a date-only endDate for a timed event, which reads as "ends at
    # midnight" rather than "end unknown".
    if event.end_date is not None or event.end_time is not None:
        end = _combine(event.end_date or event.start_date, event.end_time)
        if end and end != start:
            data["endDate"] = end

    if event.description:
        data["description"] = event.description
    if event.image_url:
        data["image"] = event.image_url

    venue = event.corrected_venue or event.venue
    address = event.corrected_address or event.address
    if venue or address:
        place: dict = {"@type": "Place", "name": venue or address}
        if address:
            place["address"] = {"@type": "PostalAddress", "streetAddress": address}
            if event.city:
                place["address"]["addressLocality"] = event.city.name
        latitude = event.corrected_latitude or event.geocoded_latitude or event.latitude
        longitude = event.corrected_longitude or event.geocoded_longitude or event.longitude
        if latitude is not None and longitude is not None:
            place["geo"] = {
                "@type": "GeoCoordinates",
                "latitude": latitude,
                "longitude": longitude,
            }
        data["location"] = place

    if event.source:
        data["organizer"] = {"@type": "Organization", "name": event.source}
    if event.canonical_url:
        data["sameAs"] = event.canonical_url

    return data


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


def city_listing_structured_data(city, events: list[Event], *, page_url: str) -> str:
    """A town page as a schema.org ItemList of the events it shows, so a crawler
    (a search engine or an AI tool) gets every event's details from the one
    listing page without opening each event."""
    schools = [u.name for u in city.universities]
    return script_safe_json(
        {
            "@context": "https://schema.org",
            "@type": "ItemList",
            "name": f"Events in {city.name}",
            "url": page_url,
            "about": {
                "@type": "City",
                "name": city.name,
                **(
                    {"containedInPlace": {"@type": "State", "name": city.state_or_region}}
                    if city.state_or_region
                    else {}
                ),
            },
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
