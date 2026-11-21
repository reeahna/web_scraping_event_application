"""Year-less listing dates ("Nov 20", "Fri, Dec 4") resolve against the town's
own date, never the server's UTC date, and a day that has only just passed is
not pushed to next year.

Production runs on UTC. Before this, a source in Central time imported at
8 PM local (02:00 UTC the next day) read "Nov 20" against Nov 21 and stored
that evening's event as Nov 20 of the following year.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.extraction import normalize
from app.extraction.normalize import local_today, normalize_candidate
from app.extraction.transform import _next_occurrence, _parse_date, apply_transformations
from app.extraction.types import EventCandidate
from app.schemas.extraction import SiteConfiguration, TransformationRuleConfig

YEARLESS = [
    {
        "field": "start_datetime",
        "kind": "parse_date",
        "params": {"formats": ["%b %d"], "assume_next_occurrence": True},
    }
]


def _config(timezone: str | None) -> SiteConfiguration:
    return SiteConfiguration(
        pattern_name="generic_html_cards",
        listing_url="https://example.com/events",
        timezone=timezone,
        transformations=YEARLESS,
    )


def _candidate(start: str) -> EventCandidate:
    return EventCandidate(
        raw={"title": "Show", "canonical_url": "https://example.com/e/1",
             "start_datetime": start},
        title=None, canonical_url=None, description=None,
        start_date=None, start_time=None, end_date=None, end_time=None,
        timezone=None, venue=None, address=None, image_url=None,
        latitude=None, longitude=None, source_category=None, external_source_id=None,
        field_source_paths={}, transformation_history=(),
        source_page="https://example.com/events", extraction_pattern="generic_html_cards",
        warnings=(), raw_record_hash="deadbeef",
    )


@pytest.fixture
def utc_now(monkeypatch):
    def set_now(value: datetime) -> None:
        monkeypatch.setattr(normalize, "_utc_now", lambda: value)
    return set_now


# --- the town's date ------------------------------------------------------------


@pytest.mark.parametrize(
    "now,zone,expected",
    [
        # US evening: already tomorrow in UTC, still today in town.
        (datetime(2026, 11, 21, 2, 0, tzinfo=UTC), "America/Chicago", date(2026, 11, 20)),
        (datetime(2026, 11, 21, 4, 59, tzinfo=UTC), "America/New_York", date(2026, 11, 20)),
        (datetime(2026, 11, 21, 5, 0, tzinfo=UTC), "America/New_York", date(2026, 11, 21)),
        # New Year's Eve in Central time is New Year's Day in UTC.
        (datetime(2027, 1, 1, 3, 0, tzinfo=UTC), "America/Chicago", date(2026, 12, 31)),
        # Spring forward (Mar 14, 2027): local midnight is 05:00 UTC before
        # the change; at 06:30 UTC it is 01:30 EST on the 14th.
        (datetime(2027, 3, 14, 4, 59, tzinfo=UTC), "America/New_York", date(2027, 3, 13)),
        (datetime(2027, 3, 14, 6, 30, tzinfo=UTC), "America/New_York", date(2027, 3, 14)),
        # Fall back (Nov 1, 2026): local midnight is 04:00 UTC under EDT.
        (datetime(2026, 11, 1, 3, 59, tzinfo=UTC), "America/New_York", date(2026, 10, 31)),
        (datetime(2026, 11, 1, 4, 0, tzinfo=UTC), "America/New_York", date(2026, 11, 1)),
        # Indiana's own zone name, used for Bloomington.
        (datetime(2026, 7, 4, 3, 30, tzinfo=UTC), "America/Indiana/Indianapolis",
         date(2026, 7, 3)),
        # A town still on the UTC default, or an unknown zone, uses UTC.
        (datetime(2026, 11, 21, 2, 0, tzinfo=UTC), "UTC", date(2026, 11, 21)),
        (datetime(2026, 11, 21, 2, 0, tzinfo=UTC), None, date(2026, 11, 21)),
        (datetime(2026, 11, 21, 2, 0, tzinfo=UTC), "Not/AZone", date(2026, 11, 21)),
    ],
)
def test_local_today(utc_now, now, zone, expected):
    utc_now(now)
    assert local_today(zone) == expected


# --- resolving a year-less date ------------------------------------------------------


@pytest.mark.parametrize(
    "month,day,reference,expected",
    [
        (11, 20, date(2026, 11, 20), date(2026, 11, 20)),  # today
        (11, 25, date(2026, 11, 20), date(2026, 11, 25)),  # upcoming
        (11, 19, date(2026, 11, 20), date(2026, 11, 19)),  # yesterday, not 2027
        (11, 13, date(2026, 11, 20), date(2026, 11, 13)),  # a week ago, still this year
        (11, 12, date(2026, 11, 20), date(2027, 11, 12)),  # longer ago: next year
        (1, 2, date(2026, 12, 30), date(2027, 1, 2)),  # next week, over the new year
        (12, 30, date(2027, 1, 2), date(2026, 12, 30)),  # three days ago, last year
        (12, 31, date(2027, 1, 1), date(2026, 12, 31)),  # last night
        (6, 1, date(2027, 1, 2), date(2027, 6, 1)),
    ],
)
def test_next_occurrence(month, day, reference, expected):
    assert _next_occurrence(month, day, reference) == expected


def test_rule_with_its_own_reference_date_keeps_it():
    rule = TransformationRuleConfig.model_validate(
        {**YEARLESS[0], "params": {**YEARLESS[0]["params"], "reference_date": "2026-08-06"}}
    )
    value, _ = apply_transformations("Jan 05", [rule], reference_date=date(2026, 12, 30))
    assert value == date(2027, 1, 5)


def test_dated_values_are_never_moved():
    params = {"formats": ["%b %d %Y"], "assume_next_occurrence": True,
              "reference_date": "2026-11-21"}
    assert _parse_date("Nov 20 2026", params) == date(2026, 11, 20)


# --- through the normalizer, as an import sees it -------------------------------


def test_evening_import_keeps_tonights_event_this_year(utc_now):
    """8 PM in Champaign on Nov 20 is 02:00 UTC on Nov 21."""
    utc_now(datetime(2026, 11, 21, 2, 0, tzinfo=UTC))
    result = normalize_candidate(_candidate("Nov 20"), _config("America/Chicago"))
    assert result.start_date == date(2026, 11, 20)


def test_town_timezone_comes_from_the_city_when_the_source_has_none(utc_now):
    utc_now(datetime(2026, 11, 21, 2, 0, tzinfo=UTC))
    result = normalize_candidate(
        _candidate("Nov 20"), _config(None), fallback_timezone="America/Chicago"
    )
    assert result.start_date == date(2026, 11, 20)


def test_new_years_eve_import(utc_now):
    utc_now(datetime(2027, 1, 1, 3, 0, tzinfo=UTC))  # 9 PM Dec 31 in Chicago
    config = _config("America/Chicago")
    assert normalize_candidate(_candidate("Dec 31"), config).start_date == date(2026, 12, 31)
    assert normalize_candidate(_candidate("Jan 01"), config).start_date == date(2027, 1, 1)
    assert normalize_candidate(_candidate("Jan 15"), config).start_date == date(2027, 1, 15)


def test_morning_after_new_year_reads_last_week_as_last_year(utc_now):
    utc_now(datetime(2027, 1, 2, 15, 0, tzinfo=UTC))
    config = _config("America/New_York")
    assert normalize_candidate(_candidate("Dec 30"), config).start_date == date(2026, 12, 30)
    assert normalize_candidate(_candidate("Dec 20"), config).start_date == date(2027, 12, 20)
