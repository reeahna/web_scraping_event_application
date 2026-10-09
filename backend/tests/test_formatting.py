from datetime import date, time

from app.core.formatting import human_date, human_date_long, human_time


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
