"""Deterministic date/time format inference.

Fits observed card text against a bounded, ordered table of strptime formats
and returns the smallest set that covers the samples. There is no free-form
date guesser here and no `dateutil`-style fuzzy parsing: a value either
matches a format in the table or it does not.

Two rules the rest of the engine depends on:

* Whitespace is normalized before any format is tried, because
  `resolve_css` joins a nested `<span>` date with single spaces — so
  "Tuesday | Oct  6 ,\\n 2026" is tested as "Tuesday | Oct 6 , 2026".
* A missing year: when no sample carries a four-digit year, a year-less
  format is inferred (from YEARLESS_DATE_FORMAT_TABLE) and flagged
  `year_assumed`, so the caller can parse it as the next occurrence (this
  year if still upcoming, else next). Only if no year-less format matches
  either does inference give up with `no_year_in_text`.
"""

from __future__ import annotations

import re
from datetime import datetime

from app.extraction.inference.types import DateFormatCandidate

# Ordered most-specific-first. Every entry is a real strptime format; the
# comma/pipe-spaced variants exist because BeautifulSoup's `get_text(" ")`
# inserts a space between nested spans, which is exactly the shape the
# extraction engine will see at runtime.
DATE_FORMAT_TABLE: tuple[str, ...] = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m/%d/%Y",
    "%m/%d/%y",
    "%m-%d-%Y",
    "%B %d, %Y",
    "%B %d , %Y",
    "%b %d, %Y",
    "%b %d , %Y",
    "%b. %d, %Y",
    "%B %d %Y",
    "%b %d %Y",
    "%d %B %Y",
    "%d %b %Y",
    "%A, %B %d, %Y",
    "%A, %b %d, %Y",
    "%a, %B %d, %Y",
    "%a, %b %d, %Y",
    "%A %B %d, %Y",
    "%A %b %d, %Y",
    "%a %B %d, %Y",
    "%a %b %d, %Y",
    "%A | %B %d , %Y",
    "%A | %b %d , %Y",
    "%A | %B %d, %Y",
    "%A | %b %d, %Y",
    "%a | %B %d , %Y",
    "%a | %b %d , %Y",
    "%A - %B %d, %Y",
    "%A - %b %d, %Y",
    # A separator before the year instead of a comma. Common on card layouts
    # that render month/day/year as separate elements and join them with a
    # slash or bullet — the same shape as the comma variants above, so it
    # belongs in the table rather than in a special case.
    "%B %d / %Y",
    "%b %d / %Y",
    "%A | %B %d / %Y",
    "%A | %b %d / %Y",
    "%B %d · %Y",
    "%b %d · %Y",
    "%B %d | %Y",
    "%b %d | %Y",
    # Datetime cards that render a weekday + date + time in one string, joined
    # by "@" ("Sunday Sep 6, 2026 @ 07:00 PM"). Parsed to a date here; the time
    # is carried through the same value if a start_time field is also inferred.
    "%A %b %d, %Y @ %I:%M %p",
    "%A %B %d, %Y @ %I:%M %p",
    "%a %b %d, %Y @ %I:%M %p",
    "%b %d, %Y @ %I:%M %p",
    "%B %d, %Y @ %I:%M %p",
)

# Year-less date shapes, tried only when no sampled value carries a year. A
# month name is always required, so these never match a bare number. Paired
# with assume_next_occurrence, they resolve to the current or next year.
YEARLESS_DATE_FORMAT_TABLE: tuple[str, ...] = (
    "%d %B %A",
    "%d %b %A",
    "%A %d %B",
    "%A %d %b",
    "%B %d %A",
    "%b %d %A",
    "%A %B %d",
    "%A %b %d",
    "%a %B %d",
    "%a %b %d",
    "%A, %B %d",
    "%A, %b %d",
    "%a, %B %d",
    "%a, %b %d",
    "%d %B",
    "%d %b",
    "%B %d",
    "%b %d",
    "%b. %d",
)

TIME_FORMAT_TABLE: tuple[str, ...] = (
    "%I:%M %p",
    "%I:%M%p",
    "%I:%M:%S %p",
    "%I %p",
    "%H:%M",
    "%H:%M:%S",
)

_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_ALL_DAY_RE = re.compile(r"\ball[\s-]?day\b", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ].*)?$")

# Generic date substrings worth pulling out of a longer blob (e.g. a card
# that renders "Fri Jul 24, 2026 + Add to calendar"). Each is a plain
# capture of a date-shaped run — never a site-specific pattern.
DATE_SUBSTRING_PATTERNS: tuple[str, ...] = (
    r"(\d{4}-\d{2}-\d{2})",
    r"([A-Z][a-z]{2,8} \d{1,2},? \d{4})",
    r"([A-Z][a-z]{2,8} [A-Z][a-z]{2,8} \d{1,2},? \d{4})",
    r"([A-Z][a-z]{2,8}\.? \d{1,2},? \d{4})",
    r"(\d{1,2}/\d{1,2}/\d{2,4})",
)

# Year-less date substrings for a date embedded in prose with no year
# ("...Sunday, October 25th (Rain date...)", "Market: Saturday, September 19;
# 10 AM"). Each captured substring is validated against YEARLESS_DATE_FORMAT_TABLE
# (ordinals tolerated), so a non-date capture is discarded. The first match in a
# blob wins, which is the primary date on the cards that carry more than one.
# The ordinal group is a *capturing* group, not "(?:...)", because the shared
# ReDoS validator (app.core.safe_regex) forbids "(?" in a stored extraction
# regex; the outer group 1 still captures the whole date.
YEARLESS_DATE_SUBSTRING_PATTERNS: tuple[str, ...] = (
    r"([A-Z][a-z]{2,8},?\s+[A-Z][a-z]{2,8}\.?\s+\d{1,2}(st|nd|rd|th)?)",
    r"([A-Z][a-z]{2,8}\.?\s+\d{1,2}(st|nd|rd|th)?)",
    r"(\d{1,2}(st|nd|rd|th)?\s+[A-Z][a-z]{2,8})",
)

TIME_SUBSTRING_PATTERN = r"(\d{1,2}:\d{2}\s*[APap]\.?[Mm]\.?)"

# A range whose two halves are each a complete, independently parseable
# date. Only such a range is ever split — "Sep 12 - 13, 2026" has no
# explicit second date and is deliberately left alone.
_RANGE_SPLIT_RE = re.compile(r"^(.+?)\s*(?:—|–|-|\bto\b|\bthrough\b)\s*(.+)$", re.IGNORECASE)


def normalize_whitespace(value: str | None) -> str:
    return " ".join(str(value).split()) if value is not None else ""


_ORDINAL_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\b", re.IGNORECASE)


def strip_ordinals(value: str) -> str:
    """Drop English ordinal suffixes from day numbers ("Sep 6th" -> "Sep 6",
    "the 1st" -> "the 1"), which strptime cannot parse. Shared by date-format
    inference and the runtime parser so a card that writes "6th" is still
    read as a date."""
    return _ORDINAL_RE.sub(r"\1", value)


def has_year(value: str | None) -> bool:
    return bool(_YEAR_RE.search(normalize_whitespace(value)))


def is_all_day(value: str | None) -> bool:
    return bool(_ALL_DAY_RE.search(normalize_whitespace(value)))


def _hits(values: list[str], fmt: str) -> int:
    return sum(1 for value in values if _matches(value, fmt))


def _matches(value: str, fmt: str) -> bool:
    try:
        datetime.strptime(strip_ordinals(value), fmt)
    except ValueError:
        return False
    return True


def _is_iso(value: str) -> bool:
    return bool(_ISO_DATE_RE.match(value))


def _fit(values: list[str], table: tuple[str, ...]) -> list[tuple[str, int]]:
    """(format, hit_count) for every format matching at least one value,
    ordered by hit count then by the table's own order — deterministic for
    identical input, never dependent on dict/set iteration."""
    scored: list[tuple[str, int]] = []
    for fmt in table:
        hits = sum(1 for value in values if _matches(value, fmt))
        if hits:
            scored.append((fmt, hits))
    scored.sort(key=lambda item: (-item[1], table.index(item[0])))
    return scored


def _cover(values: list[str], table: tuple[str, ...], limit: int) -> list[str]:
    """Greedy minimal cover: repeatedly take the format matching the most
    still-unmatched values."""
    remaining = list(values)
    chosen: list[str] = []
    while remaining and len(chosen) < limit:
        scored = _fit(remaining, table)
        if not scored:
            break
        best = scored[0][0]
        chosen.append(best)
        remaining = [v for v in remaining if not _matches(v, best)]
    return chosen


def infer_date_formats(
    raw_values: list[str | None], *, max_formats: int = 4, max_samples: int = 3
) -> tuple[list[DateFormatCandidate], float]:
    """Returns (candidates, match_rate). A candidate with `format` set is
    written into `SiteConfiguration.date_formats`; the ISO candidate is
    reported for evidence but needs no configuration, since
    `parse_date_value` already falls back to ISO 8601."""
    values = [strip_ordinals(normalize_whitespace(v)) for v in raw_values if normalize_whitespace(v)]
    if not values:
        return [], 0.0

    samples = tuple(values[:max_samples])
    iso_hits = sum(1 for v in values if _is_iso(v))
    if iso_hits == len(values):
        return [
            DateFormatCandidate(
                kind="date",
                format="",
                match_rate=1.0,
                samples=samples,
                evidence=("values are already ISO 8601 — no strptime format needed",),
                warnings=(),
                accepted=True,
            )
        ], 1.0

    if not any(has_year(v) for v in values):
        # No year in the text. Rather than give up, infer a year-less format and
        # let the config assume the current/next occurrence at parse time — the
        # right reading for an upcoming-events listing that omits the year. The
        # `year_assumed` warning tells the proposer to emit a parse_date
        # transformation with assume_next_occurrence rather than a plain format.
        chosen = _cover(values, YEARLESS_DATE_FORMAT_TABLE, max_formats)
        if chosen:
            matched = sum(1 for v in values if any(_matches(v, fmt) for fmt in chosen))
            match_rate = matched / len(values)
            return [
                DateFormatCandidate(
                    kind="date",
                    format=fmt,
                    match_rate=sum(1 for v in values if _matches(v, fmt)) / len(values),
                    samples=tuple(v for v in values if _matches(v, fmt))[:max_samples],
                    evidence=(
                        f"matches {_hits(values, fmt)}/{len(values)} samples; no year in "
                        "text, so the current/next occurrence is assumed",
                    ),
                    warnings=("year_assumed",),
                    accepted=True,
                )
                for fmt in chosen
            ], match_rate
        return [
            DateFormatCandidate(
                kind="date",
                format="",
                match_rate=0.0,
                samples=samples,
                evidence=("no four-digit year present in any sampled value",),
                warnings=("no_year_in_text",),
                accepted=False,
            )
        ], 0.0

    chosen = _cover(values, DATE_FORMAT_TABLE, max_formats)
    if not chosen:
        return [
            DateFormatCandidate(
                kind="date",
                format="",
                match_rate=0.0,
                samples=samples,
                evidence=("no format in the inference table matched the sampled values",),
                warnings=("unrecognized_date_shape",),
                accepted=False,
            )
        ], 0.0

    matched = sum(1 for v in values if any(_matches(v, fmt) for fmt in chosen))
    match_rate = matched / len(values)
    candidates = [
        DateFormatCandidate(
            kind="date",
            format=fmt,
            match_rate=sum(1 for v in values if _matches(v, fmt)) / len(values),
            samples=tuple(v for v in values if _matches(v, fmt))[:max_samples],
            evidence=(f"matches {_hits(values, fmt)}/{len(values)} samples",),
            warnings=(),
            accepted=True,
        )
        for fmt in chosen
    ]
    return candidates, match_rate


def infer_time_formats(
    raw_values: list[str | None], *, max_formats: int = 3, max_samples: int = 3
) -> tuple[list[DateFormatCandidate], float]:
    values = [normalize_whitespace(v) for v in raw_values if normalize_whitespace(v)]
    if not values:
        return [], 0.0

    samples = tuple(values[:max_samples])
    if all(is_all_day(v) for v in values):
        return [
            DateFormatCandidate(
                kind="time",
                format="",
                match_rate=0.0,
                samples=samples,
                evidence=("every sampled value is an all-day marker",),
                warnings=("all_day_only",),
                accepted=False,
            )
        ], 0.0

    chosen = _cover(values, TIME_FORMAT_TABLE, max_formats)
    if not chosen:
        return [], 0.0

    matched = sum(1 for v in values if any(_matches(v, fmt) for fmt in chosen))
    candidates = [
        DateFormatCandidate(
            kind="time",
            format=fmt,
            match_rate=sum(1 for v in values if _matches(v, fmt)) / len(values),
            samples=tuple(v for v in values if _matches(v, fmt))[:max_samples],
            evidence=(f"matches {_hits(values, fmt)}/{len(values)} samples",),
            warnings=(),
            accepted=True,
        )
        for fmt in chosen
    ]
    return candidates, matched / len(values)


def infer_extraction_pattern(
    raw_values: list[str | None], patterns: tuple[str, ...], table: tuple[str, ...]
) -> tuple[str, float] | None:
    """When a value doesn't parse whole but contains a parseable date/time
    substring, returns (regex, match_rate) for the best generic extraction
    pattern — the basis of an inferred `regex_extract_group` transformation."""
    values = [normalize_whitespace(v) for v in raw_values if normalize_whitespace(v)]
    if not values:
        return None

    best: tuple[str, float] | None = None
    for pattern in patterns:
        compiled = re.compile(pattern)
        extracted = [m.group(1) for m in (compiled.search(v) for v in values) if m]
        if not extracted:
            continue
        parseable = sum(
            1 for value in extracted if any(_matches(value, fmt) for fmt in table) or _is_iso(value)
        )
        rate = parseable / len(values)
        if rate > 0 and (best is None or rate > best[1]):
            best = (pattern, rate)
    return best


def split_explicit_range(value: str) -> tuple[str, str] | None:
    """Splits a range only when *both* halves independently parse as a
    complete date. "Sep 12 - 13, 2026" returns None (the second half carries
    no month) — a range with an implicit endpoint is never expanded."""
    normalized = normalize_whitespace(value)
    match = _RANGE_SPLIT_RE.match(normalized)
    if not match:
        return None
    start, end = match.group(1).strip(), match.group(2).strip()
    for half in (start, end):
        if not (any(_matches(half, fmt) for fmt in DATE_FORMAT_TABLE) or _is_iso(half)):
            return None
    return start, end
