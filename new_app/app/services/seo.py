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


def event_structured_data(event: Event) -> str:
    """A schema.org/Event JSON-LD document for one public event.

    Only fields the public page already shows are included: this describes the
    page, it does not publish anything the visitor could not otherwise read.
    """
    data: dict = {
        "@context": "https://schema.org",
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

    # ensure_ascii keeps the payload valid inside the <script> block whatever
    # the source text contains.
    return json.dumps(data, ensure_ascii=True)


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
