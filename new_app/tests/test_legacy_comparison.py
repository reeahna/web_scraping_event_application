"""Phase 16: legacy comparison engine, read-only reader, and migration status."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest

from app.extraction.types import EventCandidate
from app.services.legacy_comparison import (
    ComparableEvent,
    LegacyUnavailable,
    candidate_to_comparable,
    compare_events,
    default_legacy_db_path,
    read_legacy_events,
    set_migration_status,
)


def _ev(title, start, **over):
    base = dict(
        end_date=None, start_time=None, venue=None, address=None, url=None,
        category=None, image_url=None, has_recurrence=False, valid=True,
    )
    base.update(over)
    return ComparableEvent(title=title, start_date=start, **base)


def test_compare_matched_legacy_only_new_only():
    legacy = [_ev("Jazz Night", "2026-09-01"), _ev("Old Gala", "2026-09-02")]
    new = [_ev("Jazz Night", "2026-09-01"), _ev("New Fest", "2026-09-03")]
    report = compare_events(legacy, new, legacy_source="Src")
    assert report.matched_count == 1
    assert {e.title for e in report.legacy_only} == {"Old Gala"}
    assert {e.title for e in report.new_only} == {"New Fest"}


def test_field_differences_are_reported():
    legacy = [_ev("Show", "2026-09-01", venue="Old Hall", url="http://a")]
    new = [_ev("Show", "2026-09-01", venue="New Hall", url="http://a")]
    report = compare_events(legacy, new, legacy_source="Src")
    assert report.field_difference_count == 1
    assert report.matched[0].field_differences == ["venue"]


def test_likely_duplicates_detected():
    legacy = [_ev("Dup", "2026-09-01"), _ev("Dup", "2026-09-01")]
    report = compare_events(legacy, [], legacy_source="Src")
    assert len(report.likely_duplicate_keys) == 1


def test_validation_differences():
    new = [_ev("Bad", "2026-09-01", valid=False), _ev("Good", "2026-09-02", valid=True)]
    report = compare_events([], new, legacy_source="Src")
    assert len(report.new_invalid) == 1
    assert report.new_invalid[0].title == "Bad"


def test_recurrence_difference():
    legacy = [_ev("Weekly", "2026-09-01", has_recurrence=False)]
    new = [_ev("Weekly", "2026-09-01", has_recurrence=True)]
    report = compare_events(legacy, new, legacy_source="Src")
    assert "recurrence" in report.matched[0].field_differences


def test_read_legacy_events_is_read_only_and_returns_data():
    path = default_legacy_db_path()
    if not path.exists():
        pytest.skip("legacy events.db is not present in this environment")
    events = read_legacy_events("Eventbrite")
    assert len(events) > 0
    assert all(isinstance(e, ComparableEvent) for e in events)
    # Unknown source is simply empty, not an error.
    assert read_legacy_events("no-such-source-xyz") == []


def test_reader_connection_cannot_write():
    path = default_legacy_db_path()
    if not path.exists():
        pytest.skip("legacy events.db is not present in this environment")
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("INSERT INTO events (title) VALUES ('should not write')")
    finally:
        con.close()


def test_missing_legacy_db_is_unavailable(tmp_path):
    with pytest.raises(LegacyUnavailable):
        read_legacy_events("Src", db_path=Path(tmp_path) / "does-not-exist.db")


def test_candidate_to_comparable_maps_fields():
    candidate = EventCandidate(
        raw={}, title="Mapped", canonical_url="https://x/e", description=None,
        start_date=date(2026, 9, 1), start_time=None, end_date=None, end_time=None,
        timezone=None, venue="Hall", address="1 St", image_url="http://i",
        latitude=None, longitude=None, source_category="Music", external_source_id=None,
        field_source_paths={}, transformation_history=(), source_page="https://x",
        extraction_pattern="p", warnings=(), raw_record_hash="h",
        recurrence_parent_id="P1",
    )
    c = candidate_to_comparable(candidate, valid=True)
    assert c.title == "Mapped"
    assert c.start_date == "2026-09-01"
    assert c.url == "https://x/e"
    assert c.category == "Music"
    assert c.has_recurrence is True


def test_set_migration_status(make_city, make_website, db_session):
    city = make_city()
    website = make_website(city)
    set_migration_status(db_session, website, "migrated", legacy_source="Eventbrite")
    db_session.refresh(website)
    assert website.legacy_migration_status == "migrated"
    assert website.legacy_source_name == "Eventbrite"
    assert website.legacy_migrated_at is not None

    set_migration_status(db_session, website, "unavailable")
    db_session.refresh(website)
    assert website.legacy_migration_status == "unavailable"
    assert website.legacy_migrated_at is None


def test_set_status_endpoint(client, make_super_admin, login, make_city, make_website):
    make_super_admin(email="root@example.com", password="root-pass-1234")
    login("root@example.com", "root-pass-1234")
    city = make_city()
    website = make_website(city)
    csrf = client.cookies.get("csrf_token")
    resp = client.post(
        f"/admin/websites/{website.id}/legacy-comparison/status",
        data={"csrf_token": csrf, "status": "migrated", "legacy_source": "Eventbrite"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
