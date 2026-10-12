"""Cards that print date and time in one element ("Thu, Oct. 15, 2026 | 1:30PM").

The date is read through the date-substring extraction; the time must be read
from the same element rather than the event being imported as date-only.
"Sept." (a common US abbreviation strptime does not know) and an hour with no
minutes ("8PM") must parse too. Synthetic markup; no hostname is special.
"""

from __future__ import annotations

import asyncio
from datetime import date

import httpx

from app.extraction.inference.dates import TIME_SUBSTRING_PATTERN, strip_ordinals
from app.extraction.transform import parse_date_value
from app.services.onboarding_automation import detect_and_configure

from .extraction_helpers import patched_http_fetch

URL = "https://arts.example.edu/events"
DATES = [
    "Thu, Oct. 15, 2026 | 1:30PM", "Fri, Oct. 16, 2026 | 8PM", "Sat, Nov. 7, 2026 | 7:30PM",
    "Sun, Nov. 8, 2026 | 3PM", "Tue, Sept. 1, 2026 | 12PM", "Wed, Dec. 2, 2026 | 10:00AM",
]


def _card(i: int, when: str) -> str:
    return (
        '<div class="event-result-card livestream-card card--clickable">\n\t'
        f'<div class="event-result-card__image"><img src="/img/{i}.jpg" alt="x"></div>\n\t'
        '<div class="event-result-card__content">\n\t\t'
        '<p class="event-result-card__presenter">School of Music</p>\n\t\t'
        f'<a href="{URL}/show-{i}" class="primary-link">Show number {i}'
        '<span class="primary-link__arrow"></span></a>\n\t\t'
        f'<p class="event-result-card__date-time">\n\t\t\t\t{when}\n\t\t\t</p>\n\t'
        "</div>\n</div>"
    )


def test_combined_date_time_cards_import_with_times(db_session, make_city, make_website):
    page = "<html><body><main>" + "".join(_card(i, d) for i, d in enumerate(DATES))
    page += "</main></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        body = page if request.url.path == "/events" else "<html><body><h1>x</h1></body></html>"
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    city = make_city(name="College Town", slug="college-town", timezone="America/New_York")
    website = make_website(city, name="Arts Center", base_url=URL)
    with patched_http_fetch(handler):
        result = asyncio.run(detect_and_configure(db_session, website))

    assert result.quality.valid_count == len(DATES)
    config = result.inference.configuration
    assert config.field_selectors["start_time"].selector == (
        config.field_selectors["start_datetime"].selector
    )
    assert parse_date_value("Sept. 1, 2026", config.date_formats) == date(2026, 9, 1)


def test_time_substring_reads_hour_only_times_but_never_a_year():
    import re

    pattern = re.compile(TIME_SUBSTRING_PATTERN)
    assert pattern.search("Fri, Oct. 16, 2026 | 8PM").group(1) == "8PM"
    assert pattern.search("Thu, Oct. 15, 2026 | 1:30PM").group(1) == "1:30PM"
    assert pattern.search("Oct. 15, 2026 Amphitheater") is None
    assert pattern.search("Top 10 Amazing Shows") is None
    assert pattern.search("Doors at 7 p.m.").group(1) == "7 p.m."


def test_sept_is_read_as_september():
    assert strip_ordinals("Tue, Sept. 1st, 2026") == "Tue, Sep. 1, 2026"
    assert parse_date_value("Sept 1, 2026", ["%b %d, %Y"]) == date(2026, 9, 1)
