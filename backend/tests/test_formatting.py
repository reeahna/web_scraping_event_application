from datetime import date, time
from types import SimpleNamespace

from app.core.formatting import (
    event_time_zone,
    human_date,
    human_date_long,
    human_time,
    time_zone_abbreviation,
)


def test_human_date_includes_year():
    # Event cards show the year so multi-year listings read unambiguously.
    assert human_date(date(2026, 9, 1)) == "Tue, Sep 1, 2026"
    assert human_date(date(2027, 12, 25)) == "Sat, Dec 25, 2027"


def test_human_date_none_is_tbd():
    assert human_date(None) == "Date TBD"


def test_human_date_long_unchanged():
    assert human_date_long(date(2026, 9, 1)) == "Tuesday, September 1, 2026"


def test_human_time_no_leading_zero():
    assert human_time(time(17, 0)) == "5:00 PM"
    assert human_time(time(9, 5)) == "9:05 AM"
    assert human_time(None) == ""


def test_time_zone_abbreviation_follows_daylight_saving():
    assert time_zone_abbreviation("America/Chicago", date(2026, 10, 15), time(18, 0)) == "CDT"
    assert time_zone_abbreviation("America/Chicago", date(2026, 12, 15), time(18, 0)) == "CST"
    assert time_zone_abbreviation("America/New_York", date(2026, 7, 4), None) == "EDT"


def test_time_zone_abbreviation_blank_when_unset_or_unknown():
    # UTC is the default for a town nobody has configured, so it is not shown.
    assert time_zone_abbreviation("UTC", date(2026, 7, 4), None) == ""
    assert time_zone_abbreviation(None, date(2026, 7, 4), None) == ""
    assert time_zone_abbreviation("Not/AZone", date(2026, 7, 4), None) == ""


def test_event_time_zone_prefers_event_then_town():
    city = SimpleNamespace(timezone="America/Indiana/Indianapolis")
    day = date(2026, 10, 15)
    assert event_time_zone(
        SimpleNamespace(timezone=None, city=city, start_date=day, start_time=None)
    ) == "EDT"
    assert event_time_zone(
        SimpleNamespace(timezone="America/Denver", city=city, start_date=day, start_time=None)
    ) == "MDT"
    assert event_time_zone(
        SimpleNamespace(timezone=None, city=None, start_date=day, start_time=None)
    ) == ""
