"""The operational dashboard renders values, not storage representations.

Timestamps reached the page as ISO strings with microseconds
("2026-09-16T20:09:43.733332") and state names as raw identifiers
("needs_review"), because the recent-activity tables render whatever column
keys they are handed.
"""

import re
from datetime import UTC, datetime

import pytest

from app.core.permissions import SUPER_ADMINISTRATOR
from app.core.templating import _admin_datetime
from app.models.extraction_run import ExtractionRun

ISO_WITH_MICROSECONDS = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\.\d+")


@pytest.fixture
def dashboard_html(client, make_super_admin, make_city, make_website, login, db_session):
    make_super_admin(email="ops@example.com", password="ops-pass-1234")
    city = make_city(name="Ops City", slug="ops-city")
    db_session.commit()
    website = make_website(city, name="Ops Source")
    website.onboarding_status = "needs_review"
    db_session.commit()
    db_session.add(
        ExtractionRun(
            website_id=website.id,
            configuration_version=1,
            pattern_name="wordpress_rest",
            run_type="scheduled",
            status="failed",
            source_url="https://example.org/events",
            events_found=10,
            events_valid=8,
            events_rejected=2,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
    )
    db_session.commit()
    login("ops@example.com", "ops-pass-1234")
    response = client.get("/admin/reports")
    assert response.status_code == 200
    return response.text


def test_no_raw_iso_timestamps_anywhere(dashboard_html):
    match = ISO_WITH_MICROSECONDS.search(dashboard_html)
    assert match is None, f"raw timestamp on the page: {match.group(0) if match else ''}"


def test_generated_at_is_formatted(dashboard_html):
    assert re.search(r"Generated \w{3} \d+, \d{4} at ", dashboard_html)


def test_source_states_read_as_words(dashboard_html):
    assert "Needs review" in dashboard_html
    assert ">needs_review<" not in dashboard_html


def test_zero_counts_are_dimmed_rather_than_hidden(dashboard_html):
    """This page is monitored: "0 failing" is a thing the reader came to
    confirm, so a zero stays visible but must not compete with real numbers."""
    assert "metric-card is-zero" in dashboard_html
    assert dashboard_html.count("metric-card") >= 13


def test_scheduler_and_ai_health_are_badged(dashboard_html):
    assert "Not detected" in dashboard_html
    # The provider is itself named "disabled" when off; naming it beside the
    # badge read "Disabled disabled".
    assert "Disabled disabled" not in dashboard_html


@pytest.mark.parametrize(
    "value,expected_kind",
    [
        ("2026-09-16T20:09:43.733332", "formatted"),
        ("2026-09-16 20:09:44.096959+00:00", "formatted"),
        (datetime(2026, 9, 16, 20, 9, tzinfo=UTC), "formatted"),
        ("not a date at all", "passthrough"),
        (None, "dash"),
    ],
)
def test_admin_datetime_handles_every_shape_it_is_given(value, expected_kind):
    """The reporting service calls .isoformat(), JSON columns hold strings, and
    ORM columns hold datetimes. All three reach this filter."""
    result = _admin_datetime(value)
    if expected_kind == "formatted":
        assert " at " in result
        assert ISO_WITH_MICROSECONDS.search(result) is None
    elif expected_kind == "passthrough":
        assert result == value
    else:
        assert result == "\u2014"
