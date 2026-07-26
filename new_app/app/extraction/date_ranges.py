"""Shared, closed date-range parser.

A single deterministic parser for the explicit multi-day date forms that event
listings use, producing a start date, an optional end date, and a provenance
label. It is shared-core: every pattern and the inference layer call the same
function — there are no per-site or per-pattern date heuristics.

What it deliberately does NOT do (per the Phase 8G contract):

* It never *invents* a missing component. If neither side of a range carries a
  year, the range is rejected as ambiguous rather than assuming the current
  year. A missing month on the start, a reversed range (end before start), and
  an impossible calendar date are all rejected, not repaired.
* It is not a general date-expression language. It recognises an enumerated set
  of forms with plain regexes and calendar arithmetic — no `eval`, no
  configurable grammar, no natural-language ("next Friday") resolution.

Supported forms (each with a stable `form` provenance label):

    Sep 29 - 30 / 2026            shared month + shared trailing year
    Sep 29-30, 2026               shared month + shared year (comma)
    September 29 - October 1, 2026    shared trailing year, explicit months
    Sep 29, 2026 - Oct 1, 2026    fully explicit on both sides
    2026-09-29/2026-10-01         ISO range
    Mon, Sep 29 - Wed, Oct 1, 2026    weekday-prefixed explicit range
    September 29, 2026            single date (start only)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

_MONTHS: dict[str, int] = {}
for _i, _names in enumerate(
    [
        ("january", "jan"),
        ("february", "feb"),
        ("march", "mar"),
        ("april", "apr"),
        ("may",),
        ("june", "jun"),
        ("july", "jul"),
        ("august", "aug"),
        ("september", "sep", "sept"),
        ("october", "oct"),
        ("november", "nov"),
        ("december", "dec"),
    ],
    start=1,
):
    for _n in _names:
        _MONTHS[_n] = _i

_WEEKDAYS = frozenset(
    {
        "monday", "mon", "tuesday", "tue", "tues", "wednesday", "wed", "thursday",
        "thu", "thur", "thurs", "friday", "fri", "saturday", "sat", "sunday", "sun",
    }
)

# The range separators we accept between the two sides. Spaced hyphen only —
# an unspaced hyphen is left alone so ISO dates (2026-09-29) are never split
# here (they take the dedicated ISO branch), while "Sep 29-30" still splits
# because a month/day token has no hyphen of its own.
_ISO_DATE = r"\d{4}-\d{2}-\d{2}"
_ISO_RANGE_RE = re.compile(
    rf"^\s*(?P<a>{_ISO_DATE})\s*(?:/|to|–|—|-)\s*(?P<b>{_ISO_DATE})\s*$",
    re.IGNORECASE,
)
_ISO_SINGLE_RE = re.compile(rf"^\s*(?P<d>{_ISO_DATE})\s*$")

# A single textual date side: optional weekday, optional month name, a day, and
# an optional year introduced by comma or slash. Day is the only required part;
# the caller supplies shared month/year when a side omits them.
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))
_WEEKDAY_ALT = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
_SIDE_RE = re.compile(
    rf"""^\s*
    (?:(?:{_WEEKDAY_ALT})\.?\s*,?\s+)?      # optional weekday prefix (discarded)
    (?:(?P<month>{_MONTH_ALT})\.?\s+)?       # optional month name
    (?P<day>\d{{1,2}})                       # day (required)
    (?:\s*[,/]?\s*(?P<year>\d{{4}}))?        # optional year after comma or slash
    \s*$""",
    re.IGNORECASE | re.VERBOSE,
)

# Splits "left - right" on a hyphen, en/em dash, or the word "to". ISO ranges
# and single ISO dates are matched and consumed *before* this runs, so a bare
# hyphen reaching here is a range separator (e.g. "Sep 29-30"), never an ISO
# date's internal hyphen.
_RANGE_SPLIT_RE = re.compile(r"\s*[-–—]\s*|\s+to\s+", re.IGNORECASE)


@dataclass(frozen=True)
class DateRangeResult:
    """Outcome of parsing one date string.

    * `matched` true with a `start_date`: a usable single date or range.
    * `matched` false with a `reason`: rejected (ambiguous or unparseable).
      `ambiguous` distinguishes "we understood the shape but a required part was
      missing/inconsistent" (do-not-invent) from a value we simply couldn't
      read at all.
    """

    matched: bool
    start_date: date | None = None
    end_date: date | None = None
    is_range: bool = False
    form: str | None = None
    ambiguous: bool = False
    reason: str | None = None


@dataclass
class _Side:
    day: int
    month: int | None
    year: int | None


def _reject(reason: str, *, ambiguous: bool) -> DateRangeResult:
    return DateRangeResult(matched=False, ambiguous=ambiguous, reason=reason)


def _parse_side(text: str) -> _Side | None:
    match = _SIDE_RE.match(text)
    if not match:
        return None
    month_text = match.group("month")
    month = _MONTHS[month_text.lower()] if month_text else None
    year = int(match.group("year")) if match.group("year") else None
    return _Side(day=int(match.group("day")), month=month, year=year)


def _build_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_date_range(value: object) -> DateRangeResult:
    """Parse `value` into a single date or an explicit range. Deterministic:
    identical input always yields an identical result. Never raises."""
    if value is None:
        return _reject("empty", ambiguous=False)
    text = " ".join(str(value).split())
    if not text:
        return _reject("empty", ambiguous=False)

    iso = _ISO_RANGE_RE.match(text)
    if iso:
        try:
            start = date.fromisoformat(iso.group("a"))
            end = date.fromisoformat(iso.group("b"))
        except ValueError:
            return _reject("invalid_iso_date", ambiguous=True)
        if end < start:
            return _reject("reversed_range", ambiguous=True)
        return DateRangeResult(
            matched=True, start_date=start, end_date=end, is_range=True, form="iso_range"
        )

    iso_single = _ISO_SINGLE_RE.match(text)
    if iso_single:
        try:
            start = date.fromisoformat(iso_single.group("d"))
        except ValueError:
            return _reject("invalid_iso_date", ambiguous=True)
        return DateRangeResult(matched=True, start_date=start, form="iso_single")

    parts = _RANGE_SPLIT_RE.split(text, maxsplit=1)
    if len(parts) == 2:
        return _parse_textual_range(parts[0], parts[1])
    return _parse_single(text)


def _parse_single(text: str) -> DateRangeResult:
    side = _parse_side(text)
    if side is None:
        return _reject("unparseable", ambiguous=False)
    if side.month is None:
        return _reject("missing_month", ambiguous=True)
    if side.year is None:
        # Never assume the current year for a bare "September 29".
        return _reject("missing_year", ambiguous=True)
    built = _build_date(side.year, side.month, side.day)
    if built is None:
        return _reject("invalid_date", ambiguous=True)
    return DateRangeResult(matched=True, start_date=built, form="single")


def _parse_textual_range(left_text: str, right_text: str) -> DateRangeResult:
    left = _parse_side(left_text)
    right = _parse_side(right_text)
    if left is None or right is None:
        return _reject("unparseable", ambiguous=False)

    # Capture which components each side stated *explicitly*, before any
    # sharing — this is what determines the provenance label and guarantees we
    # never claim to have "shared" a value that was actually invented.
    right_had_month = right.month is not None
    left_had_year = left.year is not None
    right_had_year = right.year is not None

    # Share month and year across the sides where one omits them. We fill in
    # only from an explicitly present value on the other side — never guessed.
    if left.year is None and right.year is not None:
        left.year = right.year
    elif right.year is None and left.year is not None:
        right.year = left.year

    if right.month is None and left.month is not None:
        right.month = left.month
    # A start with no month cannot borrow one from the end without inventing
    # order across a month boundary, so it stays missing and is rejected below.

    if left.month is None or right.month is None:
        return _reject("missing_month", ambiguous=True)
    if left.year is None or right.year is None:
        return _reject("missing_year", ambiguous=True)

    start = _build_date(left.year, left.month, left.day)
    end = _build_date(right.year, right.month, right.day)
    if start is None or end is None:
        return _reject("invalid_date", ambiguous=True)
    if end < start:
        return _reject("reversed_range", ambiguous=True)

    if left_had_year and right_had_year:
        form = "explicit_both"
    elif not right_had_month:
        form = "shared_month_year"
    else:
        form = "shared_year"
    return DateRangeResult(
        matched=True, start_date=start, end_date=end, is_range=True, form=form
    )
