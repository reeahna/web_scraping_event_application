"""Parsing and validation of a bulk onboarding submission.

Pure (no Session, no I/O) so every limit and every rejection rule is directly
testable. Two input shapes are supported and nothing else: newline-separated
URLs, and a CSV whose only required column is `url`.

Safety rules enforced here:

* CSV only. No spreadsheet formats, so no macros — a `.xlsx` never reaches a
  parser, it fails decoding as text first.
* Strict decoding: UTF-8 (with or without BOM) or the submission is rejected
  whole, rather than silently mangling a row.
* Size and row caps applied *before* parsing the body.
* Formula prefixes are stripped from free-text values. Nothing in this
  application evaluates a formula, so this is purely about what gets rendered
  back into an admin's browser (and what would happen if they re-exported it).
* No configuration of any kind is accepted from an upload — the accepted
  column set is closed, and extraction configuration is not in it.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from app.core.url_canonical import canonical_url
from app.core.url_safety import UnsafeURLError, validate_public_url
from app.schemas.city import _VALID_TIMEZONES

# Closed column set. `url` is required; everything else is optional, and
# anything outside this set is ignored with a warning rather than silently
# accepted (an unknown column is far more likely to be a mistake than an
# attack, but it must never become a configuration channel).
ALLOWED_CSV_COLUMNS: frozenset[str] = frozenset(
    {"url", "name", "source_display_name", "city_slug", "timezone"}
)
REQUIRED_CSV_COLUMN = "url"

# Leading characters a spreadsheet would interpret as the start of a formula.
_FORMULA_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")

_MAX_TEXT_LENGTH = 255


@dataclass(frozen=True)
class SubmittedRow:
    row_number: int
    url: str
    normalized_url: str
    name: str | None = None
    source_display_name: str | None = None
    city_slug: str | None = None
    timezone: str | None = None


@dataclass(frozen=True)
class RejectedRow:
    row_number: int
    value: str
    reason: str

    def as_dict(self) -> dict:
        return {"row": self.row_number, "value": self.value[:300], "reason": self.reason}


@dataclass
class ParsedSubmission:
    rows: list[SubmittedRow] = field(default_factory=list)
    rejected: list[RejectedRow] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Set only when the submission is unusable as a whole (bad encoding,
    # oversized file, missing required column). Individual bad rows never
    # set this — one invalid row must not stop the valid ones.
    error: str | None = None

    @property
    def submitted_count(self) -> int:
        return len(self.rows) + len(self.rejected)


class SubmissionLimits:
    """Reads limits from Settings, but takes them as plain numbers so tests
    can tighten one without touching global configuration."""

    def __init__(
        self,
        *,
        max_urls: int,
        max_csv_rows: int,
        max_csv_bytes: int,
        max_url_length: int,
    ) -> None:
        self.max_urls = max_urls
        self.max_csv_rows = max_csv_rows
        self.max_csv_bytes = max_csv_bytes
        self.max_url_length = max_url_length


def strip_formula_prefix(value: str | None) -> str | None:
    """Neutralizes a leading formula character on untrusted free text. The
    value keeps its meaning as text; only its ability to be *interpreted* by
    a spreadsheet is removed."""
    if value is None:
        return None
    text = value.strip()
    while text and text[0] in _FORMULA_PREFIXES:
        text = text[1:].lstrip()
    return text or None


def _clean_text(value: str | None) -> str | None:
    cleaned = strip_formula_prefix(value)
    if cleaned is None:
        return None
    return " ".join(cleaned.split())[:_MAX_TEXT_LENGTH] or None


def _validate_url(raw: str, limits: SubmissionLimits) -> tuple[str, str] | str:
    """Returns (url, normalized_url) or a rejection reason string."""
    url = raw.strip()
    if not url:
        return "empty value"
    if len(url) > limits.max_url_length:
        return f"URL exceeds {limits.max_url_length} characters"
    if "://" not in url:
        url = f"https://{url}"
    try:
        url = validate_public_url(url)
    except UnsafeURLError as exc:
        return str(exc)
    normalized = canonical_url(url)
    if not normalized:
        return "URL could not be normalized"
    return url, normalized


def _accept(
    parsed: ParsedSubmission,
    *,
    row_number: int,
    raw_url: str,
    limits: SubmissionLimits,
    seen: set[str],
    name: str | None = None,
    source_display_name: str | None = None,
    city_slug: str | None = None,
    timezone: str | None = None,
) -> None:
    outcome = _validate_url(raw_url, limits)
    if isinstance(outcome, str):
        parsed.rejected.append(RejectedRow(row_number, raw_url.strip(), outcome))
        return
    url, normalized = outcome
    if normalized in seen:
        parsed.rejected.append(
            RejectedRow(row_number, url, "duplicate of an earlier row in this submission")
        )
        return
    if timezone is not None and timezone not in _VALID_TIMEZONES:
        parsed.rejected.append(
            RejectedRow(row_number, url, f"'{timezone}' is not an IANA timezone")
        )
        return
    seen.add(normalized)
    parsed.rows.append(
        SubmittedRow(
            row_number=row_number,
            url=url,
            normalized_url=normalized,
            name=name,
            source_display_name=source_display_name,
            city_slug=city_slug,
            timezone=timezone,
        )
    )


def parse_url_lines(text: str, limits: SubmissionLimits) -> ParsedSubmission:
    """Blank lines are skipped silently — a trailing newline is not an error
    an administrator should have to think about."""
    parsed = ParsedSubmission()
    lines = [line for line in (text or "").splitlines()]
    non_empty = [(i + 1, line) for i, line in enumerate(lines) if line.strip()]
    if len(non_empty) > limits.max_urls:
        parsed.error = (
            f"{len(non_empty)} URLs submitted; the maximum per batch is {limits.max_urls}."
        )
        return parsed

    seen: set[str] = set()
    for row_number, line in non_empty:
        _accept(parsed, row_number=row_number, raw_url=line, limits=limits, seen=seen)
    return parsed


def parse_csv(content: bytes, limits: SubmissionLimits) -> ParsedSubmission:
    parsed = ParsedSubmission()
    if len(content) > limits.max_csv_bytes:
        parsed.error = (
            f"The uploaded file is {len(content)} bytes; the maximum is {limits.max_csv_bytes}."
        )
        return parsed
    if b"\x00" in content[:4096]:
        parsed.error = "The uploaded file is not text. Upload a plain CSV file."
        return parsed

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        parsed.error = "The uploaded file is not valid UTF-8. Re-save it as UTF-8 CSV."
        return parsed

    try:
        reader = csv.DictReader(io.StringIO(text))
        fieldnames = reader.fieldnames or []
    except csv.Error as exc:
        parsed.error = f"The CSV could not be parsed: {exc}"
        return parsed

    columns = {(name or "").strip().casefold() for name in fieldnames}
    if REQUIRED_CSV_COLUMN not in columns:
        parsed.error = "The CSV must have a 'url' column."
        return parsed
    unknown = sorted(columns - ALLOWED_CSV_COLUMNS - {""})
    if unknown:
        parsed.warnings.append(f"Ignored unrecognized column(s): {', '.join(unknown)}")

    seen: set[str] = set()
    try:
        for index, raw_row in enumerate(reader, start=1):
            if index > limits.max_csv_rows:
                parsed.error = (
                    f"The CSV has more than {limits.max_csv_rows} rows; split it into "
                    "smaller batches."
                )
                return parsed
            row = {
                (key or "").strip().casefold(): value
                for key, value in raw_row.items()
                if key is not None
            }
            raw_url = (row.get(REQUIRED_CSV_COLUMN) or "").strip()
            if not raw_url:
                parsed.rejected.append(RejectedRow(index, "", "missing url"))
                continue
            _accept(
                parsed,
                row_number=index,
                raw_url=raw_url,
                limits=limits,
                seen=seen,
                name=_clean_text(row.get("name")),
                source_display_name=_clean_text(row.get("source_display_name")),
                city_slug=_clean_text(row.get("city_slug")),
                timezone=_clean_text(row.get("timezone")),
            )
    except csv.Error as exc:
        parsed.error = f"The CSV could not be parsed: {exc}"
        return parsed

    if not parsed.rows and not parsed.rejected:
        parsed.error = "The CSV contained no data rows."
    return parsed
