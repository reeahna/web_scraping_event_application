"""Phase 8G: the shared closed date-range parser."""

from __future__ import annotations

from datetime import date

import pytest

from app.extraction.date_ranges import parse_date_range


@pytest.mark.parametrize(
    ("text", "start", "end", "form"),
    [
        ("Sep 29 - 30 / 2026", date(2026, 9, 29), date(2026, 9, 30), "shared_month_year"),
        ("Sep 29-30, 2026", date(2026, 9, 29), date(2026, 9, 30), "shared_month_year"),
        ("Sep 29–30, 2026", date(2026, 9, 29), date(2026, 9, 30), "shared_month_year"),
        (
            "September 29 - October 1, 2026",
            date(2026, 9, 29),
            date(2026, 10, 1),
            "shared_year",
        ),
        (
            "Sep 29, 2026 - Oct 1, 2026",
            date(2026, 9, 29),
            date(2026, 10, 1),
            "explicit_both",
        ),
        ("2026-09-29/2026-10-01", date(2026, 9, 29), date(2026, 10, 1), "iso_range"),
        ("2026-09-29 - 2026-10-01", date(2026, 9, 29), date(2026, 10, 1), "iso_range"),
        (
            "Mon, Sep 29 - Wed, Oct 1, 2026",
            date(2026, 9, 29),
            date(2026, 10, 1),
            "shared_year",
        ),
        (
            "Monday September 29 - Wednesday October 1, 2026",
            date(2026, 9, 29),
            date(2026, 10, 1),
            "shared_year",
        ),
        ("Dec 30, 2026 - Jan 2, 2027", date(2026, 12, 30), date(2027, 1, 2), "explicit_both"),
    ],
)
def test_explicit_ranges_parse_with_provenance(text, start, end, form):
    result = parse_date_range(text)
    assert result.matched is True
    assert result.is_range is True
    assert result.start_date == start
    assert result.end_date == end
    assert result.form == form


@pytest.mark.parametrize(
    ("text", "start"),
    [
        ("September 29, 2026", date(2026, 9, 29)),
        ("Sep 29, 2026", date(2026, 9, 29)),
        ("2026-09-29", date(2026, 9, 29)),
        ("Tuesday, September 29, 2026", date(2026, 9, 29)),
    ],
)
def test_single_dates_parse_without_an_end(text, start):
    result = parse_date_range(text)
    assert result.matched is True
    assert result.is_range is False
    assert result.start_date == start
    assert result.end_date is None


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        # No year anywhere — never assume the current year.
        ("Sep 29 - 30", "missing_year"),
        ("September 29 - October 1", "missing_year"),
        ("September 29", "missing_year"),
        # No month anywhere.
        ("29 - 30, 2026", "missing_month"),
        # End precedes start.
        ("Oct 1, 2026 - Sep 29, 2026", "reversed_range"),
        ("2026-10-01/2026-09-29", "reversed_range"),
        # Impossible calendar date.
        ("Feb 30, 2026", "invalid_date"),
    ],
)
def test_ambiguous_or_impossible_ranges_are_rejected_not_invented(text, reason):
    result = parse_date_range(text)
    assert result.matched is False
    assert result.ambiguous is True
    assert result.reason == reason


@pytest.mark.parametrize("text", ["", "   ", None, "next weekend", "TBD"])
def test_unreadable_values_are_rejected_non_ambiguously(text):
    result = parse_date_range(text)
    assert result.matched is False
    assert result.start_date is None


def test_parsing_is_deterministic():
    a = parse_date_range("Sep 29 - 30 / 2026")
    b = parse_date_range("Sep 29 - 30 / 2026")
    assert a == b
