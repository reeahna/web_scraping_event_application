"""Submission parsing, limits and CSV safety — all pure, no Session, no I/O."""

from __future__ import annotations

from app.services.onboarding_submission import (
    SubmissionLimits,
    parse_csv,
    parse_url_lines,
    strip_formula_prefix,
)

LIMITS = SubmissionLimits(max_urls=10, max_csv_rows=5, max_csv_bytes=10_000, max_url_length=200)


def test_single_url_is_accepted():
    parsed = parse_url_lines("https://example.org/events", LIMITS)
    assert parsed.error is None
    assert len(parsed.rows) == 1
    assert parsed.rows[0].url == "https://example.org/events"
    assert parsed.rows[0].normalized_url == "https://example.org/events"


def test_multiple_urls_one_per_line():
    parsed = parse_url_lines(
        "https://a.example.org/events\nhttps://b.example.org/events\nhttps://c.example.org/e",
        LIMITS,
    )
    assert len(parsed.rows) == 3
    assert [row.row_number for row in parsed.rows] == [1, 2, 3]


def test_blank_lines_are_ignored_without_error():
    parsed = parse_url_lines(
        "\n\nhttps://a.example.org/events\n\n   \nhttps://b.example.org/e\n", LIMITS
    )
    assert parsed.error is None
    assert len(parsed.rows) == 2
    assert parsed.rejected == []


def test_scheme_is_added_when_missing():
    parsed = parse_url_lines("example.org/events", LIMITS)
    assert parsed.rows[0].url == "https://example.org/events"


def test_duplicate_url_within_one_submission_is_rejected_once():
    parsed = parse_url_lines(
        "https://example.org/events\nhttps://EXAMPLE.org/events/\nhttps://other.example.org/e",
        LIMITS,
    )
    assert len(parsed.rows) == 2
    assert len(parsed.rejected) == 1
    assert "duplicate" in parsed.rejected[0].reason


def test_one_invalid_url_does_not_reject_the_valid_ones():
    parsed = parse_url_lines(
        "https://good.example.org/events\nhttp://127.0.0.1/admin\nhttps://also-good.example.org/e",
        LIMITS,
    )
    assert len(parsed.rows) == 2
    assert len(parsed.rejected) == 1
    assert parsed.error is None


def test_unsafe_and_overlong_urls_are_rejected():
    parsed = parse_url_lines(
        "http://localhost/events\nftp://example.org/events\n" + "https://example.org/" + "x" * 300,
        LIMITS,
    )
    assert parsed.rows == []
    assert len(parsed.rejected) == 3


def test_too_many_urls_rejects_the_whole_submission():
    parsed = parse_url_lines(
        "\n".join(f"https://s{i}.example.org/events" for i in range(11)), LIMITS
    )
    assert parsed.error is not None
    assert parsed.rows == []


# --- CSV --------------------------------------------------------------------


def test_csv_with_only_a_url_column():
    content = b"url\nhttps://a.example.org/events\nhttps://b.example.org/events\n"
    parsed = parse_csv(content, LIMITS)
    assert parsed.error is None
    assert len(parsed.rows) == 2
    assert parsed.rows[0].name is None


def test_csv_optional_metadata_columns():
    content = (
        b"url,name,source_display_name,city_slug,timezone\n"
        b"https://a.example.org/events,A Hall,A Hall Presents,test-city,America/New_York\n"
    )
    parsed = parse_csv(content, LIMITS)
    row = parsed.rows[0]
    assert row.name == "A Hall"
    assert row.source_display_name == "A Hall Presents"
    assert row.city_slug == "test-city"
    assert row.timezone == "America/New_York"


def test_csv_rejects_an_invalid_timezone_per_row():
    content = b"url,timezone\nhttps://a.example.org/e,Mars/Olympus\nhttps://b.example.org/e,UTC\n"
    parsed = parse_csv(content, LIMITS)
    assert len(parsed.rows) == 1
    assert "not an IANA timezone" in parsed.rejected[0].reason


def test_csv_missing_url_column_is_rejected_as_a_whole():
    parsed = parse_csv(b"website,name\nhttps://a.example.org/e,A\n", LIMITS)
    assert parsed.error is not None
    assert "url" in parsed.error


def test_csv_unknown_columns_are_ignored_with_a_warning():
    content = b"url,configuration,notes\nhttps://a.example.org/e,{},hello\n"
    parsed = parse_csv(content, LIMITS)
    assert len(parsed.rows) == 1
    assert any("configuration" in w for w in parsed.warnings)
    # Nothing from an unknown column reaches the row.
    assert parsed.rows[0].name is None


def test_csv_row_missing_a_url_is_skipped_but_others_survive():
    content = b"url,name\n,No URL\nhttps://b.example.org/e,Fine\n"
    parsed = parse_csv(content, LIMITS)
    assert len(parsed.rows) == 1
    assert parsed.rejected[0].reason == "missing url"


def test_oversized_csv_is_rejected():
    limits = SubmissionLimits(max_urls=10, max_csv_rows=5, max_csv_bytes=20, max_url_length=200)
    parsed = parse_csv(b"url\nhttps://a.example.org/events\n", limits)
    assert parsed.error is not None
    assert "maximum" in parsed.error


def test_too_many_csv_rows_is_rejected():
    rows = b"".join(f"https://s{i}.example.org/e\n".encode() for i in range(6))
    parsed = parse_csv(b"url\n" + rows, LIMITS)
    assert parsed.error is not None


def test_non_utf8_and_binary_uploads_are_rejected_cleanly():
    assert parse_csv("url\nhttps://a.example.org/é".encode("latin-1"), LIMITS).error is not None
    assert parse_csv(b"PK\x03\x04\x00\x00binary", LIMITS).error is not None


def test_formula_prefixes_are_stripped_from_free_text():
    assert strip_formula_prefix("=cmd|'/c calc'!A1") == "cmd|'/c calc'!A1"
    assert strip_formula_prefix("+1234") == "1234"
    assert strip_formula_prefix("@SUM(A1)") == "SUM(A1)"
    assert strip_formula_prefix("Normal Name") == "Normal Name"

    content = b"url,name\nhttps://a.example.org/e,=HYPERLINK(\"http://evil\")\n"
    parsed = parse_csv(content, LIMITS)
    assert not parsed.rows[0].name.startswith("=")
