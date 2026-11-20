"""Per-source freshness for administrators, the daily-check baseline, and the
source named when it holds back a town's "Listings checked" time."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest

from app.core.onboarding import ACTIVE, ARCHIVED, BLOCKED
from app.models.audit_log import AuditLog
from app.models.extraction_run import ExtractionRun
from app.schemas.extraction import SiteConfiguration
from app.services.daily_checks import apply_daily_checks, plan_daily_checks
from app.services.event_freshness import town_listings_checked
from app.services.scheduler import get_or_create_state
from app.services.source_freshness import (
    TARGET_MET,
    TARGET_MISSING,
    TARGET_NOT_APPLICABLE,
    TARGET_OFF,
    TARGET_SLOW,
    format_age,
    source_freshness,
)

CONFIG = SiteConfiguration(pattern_name="json_ld_event", listing_url="https://example.com/events")
APPROVED = {"configuration": CONFIG.model_dump(mode="json")}
DAILY = {"enabled": True, "interval_minutes": 1440}
OFF = {"enabled": False, "interval_minutes": 1440}
NOW = datetime(2026, 11, 20, 20, 0, tzinfo=UTC)


@pytest.fixture
def town(make_city):
    return make_city(name="Bloomington", slug="bloomington", timezone="America/New_York")


def _source(db_session, make_website, city, name, *, schedule=DAILY, status=ACTIVE,
            approved_at=None, display=None):
    site = make_website(
        city, name=name, base_url=f"https://{name}.example.com", is_active=status == ACTIVE,
        approved_pattern=APPROVED, source_display_name=display,
    )
    site.onboarding_status = status
    site.schedule_config = schedule
    site.approved_at = approved_at or NOW - timedelta(days=30)
    if status == ARCHIVED:
        site.archived_at = NOW - timedelta(days=1)
    db_session.commit()
    return site


def _run(db_session, site, finished, status="success", run_type="manual", error=None):
    db_session.add(ExtractionRun(
        website_id=site.id, run_type=run_type, status=status, source_url=site.base_url,
        started_at=finished - timedelta(minutes=1), completed_at=finished,
        error_summary=error,
    ))
    db_session.commit()


def _fresh(db_session, site, now=NOW):
    return source_freshness(db_session, [site], now=now)[site.id]


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


# --- Per-source freshness ------------------------------------------------------------


def test_daily_source_checked_today_meets_target(db_session, make_website, town):
    site = _source(db_session, make_website, town, "daily")
    _run(db_session, site, NOW - timedelta(hours=3))
    state = get_or_create_state(db_session, site.id)
    state.next_run_at = NOW + timedelta(hours=21)
    db_session.commit()

    fresh = _fresh(db_session, site)
    assert fresh.target == TARGET_MET and fresh.meets_target
    assert fresh.last_success_at == NOW - timedelta(hours=3)
    assert fresh.next_check_at == NOW + timedelta(hours=21)
    assert not fresh.overdue and not fresh.failing and not fresh.needs_attention


@pytest.mark.parametrize(
    ("schedule", "target"),
    [
        (None, TARGET_MISSING),
        ({"enabled": True, "interval_minutes": 5}, TARGET_MISSING),  # invalid: below floor
        (OFF, TARGET_OFF),
        ({"enabled": True, "interval_minutes": 2880}, TARGET_SLOW),
        ({"enabled": True, "interval_minutes": 720}, TARGET_MET),
    ],
)
def test_daily_target_by_schedule(db_session, make_website, town, schedule, target):
    site = _source(db_session, make_website, town, "s", schedule=schedule)
    assert _fresh(db_session, site).target == target


@pytest.mark.parametrize("status", [BLOCKED, ARCHIVED, "needs_review"])
def test_blocked_archived_and_unapproved_sources_are_never_flagged(
    db_session, make_website, town, status
):
    site = _source(db_session, make_website, town, "x", status=status)
    _run(db_session, site, NOW - timedelta(days=60))
    fresh = _fresh(db_session, site)
    assert fresh.target == TARGET_NOT_APPLICABLE
    assert not fresh.should_be_checked and not fresh.overdue and not fresh.needs_attention


def test_overdue_only_after_interval_plus_twelve_hours(db_session, make_website, town):
    site = _source(db_session, make_website, town, "daily")
    _run(db_session, site, NOW - timedelta(hours=35))
    assert not _fresh(db_session, site).overdue  # 24h + 11h: late, not flagged
    fresh = _fresh(db_session, site, now=NOW + timedelta(hours=2))
    assert fresh.overdue and fresh.needs_attention
    assert format_age(fresh.overdue_by) == "37 hours"


def test_slow_schedule_is_held_to_its_own_interval(db_session, make_website, town):
    site = _source(db_session, make_website, town, "weekly",
                   schedule={"enabled": True, "interval_minutes": 10080})
    _run(db_session, site, NOW - timedelta(days=3))
    fresh = _fresh(db_session, site)
    assert not fresh.overdue
    assert fresh.needs_attention  # but it does not meet the daily target


def test_switched_off_source_is_held_to_the_daily_baseline(db_session, make_website, town):
    site = _source(db_session, make_website, town, "off", schedule=OFF)
    _run(db_session, site, NOW - timedelta(days=3))
    fresh = _fresh(db_session, site)
    assert fresh.overdue and fresh.next_check_at is None


def test_never_checked_source_is_measured_from_approval(db_session, make_website, town):
    fresh_site = _source(db_session, make_website, town, "new",
                         approved_at=NOW - timedelta(hours=20))
    stale_site = _source(db_session, make_website, town, "stale",
                         approved_at=NOW - timedelta(days=3))
    assert not _fresh(db_session, fresh_site).overdue
    assert _fresh(db_session, stale_site).overdue


def test_latest_failure_is_reported_and_marked_current(db_session, make_website, town):
    site = _source(db_session, make_website, town, "broken")
    _run(db_session, site, NOW - timedelta(hours=10))
    _run(db_session, site, NOW - timedelta(hours=2), status="blocked", error="HTTP 403")
    site.consecutive_failure_count = 1
    db_session.commit()
    fresh = _fresh(db_session, site)
    assert fresh.failing and fresh.needs_attention
    assert (fresh.latest_failure_status, fresh.latest_failure_summary) == ("blocked", "HTTP 403")

    _run(db_session, site, NOW - timedelta(hours=1))
    recovered = _fresh(db_session, site)
    assert not recovered.failing
    assert recovered.latest_failure_status == "blocked"  # still shown, as recovered


def test_preview_runs_are_neither_checks_nor_failures(db_session, make_website, town):
    site = _source(db_session, make_website, town, "a")
    _run(db_session, site, NOW - timedelta(hours=1), run_type="preview")
    _run(db_session, site, NOW - timedelta(hours=1), status="failed", run_type="detection")
    fresh = _fresh(db_session, site)
    assert fresh.last_success_at is None and fresh.latest_failure_at is None


def test_paused_job_has_no_next_check(db_session, make_website, town):
    site = _source(db_session, make_website, town, "a")
    state = get_or_create_state(db_session, site.id)
    state.next_run_at, state.paused = NOW + timedelta(hours=1), True
    db_session.commit()
    assert _fresh(db_session, site).next_check_at is None


# --- Town "Listings checked": which source holds it back -----------------------------


def test_failing_source_holding_back_the_town_is_named(
    client, db_session, make_website, town
):
    now = datetime.now(UTC)
    good = _source(db_session, make_website, town, "good", display="Good Calendar")
    broken = _source(db_session, make_website, town, "broken", display="Broken Calendar")
    _run(db_session, broken, now - timedelta(days=2))
    _run(db_session, broken, now - timedelta(hours=1), status="failed")
    _run(db_session, good, now - timedelta(hours=2))

    freshness = town_listings_checked(db_session, town.id)
    assert freshness.held_back_by == "failing"
    assert freshness.oldest_website_id == broken.id
    assert freshness.oldest_source_name == "Broken Calendar"
    assert freshness.others_checked_at == now - timedelta(hours=2)

    page = client.get("/city/bloomington").text
    text = _text(page)
    assert "Broken Calendar could not be checked on the latest try" in text
    assert "Every other source was checked by" in text
    assert "1 source could not be checked" not in text  # not counted twice
    assert "/admin/websites/" not in page  # visitors get the name, not an admin link


def test_overdue_source_holding_back_the_town_is_named(client, db_session, make_website, town):
    now = datetime.now(UTC)
    good = _source(db_session, make_website, town, "good")
    off = _source(db_session, make_website, town, "off", schedule=OFF, display="Old Venue")
    _run(db_session, off, now - timedelta(days=4))
    _run(db_session, good, now - timedelta(hours=2))
    assert town_listings_checked(db_session, town.id).held_back_by == "overdue"
    text = _text(client.get("/city/bloomington").text)
    assert "Old Venue has not been checked since then" in text


def test_oldest_source_that_is_merely_next_in_line_is_not_named(
    client, db_session, make_website, town
):
    now = datetime.now(UTC)
    a = _source(db_session, make_website, town, "a", display="Alpha")
    b = _source(db_session, make_website, town, "b")
    _run(db_session, a, now - timedelta(hours=20))
    _run(db_session, b, now - timedelta(hours=1))
    freshness = town_listings_checked(db_session, town.id)
    assert not freshness.held_back and freshness.oldest_website_id == a.id
    assert "Alpha" not in _text(client.get("/city/bloomington").text)


def test_other_failing_sources_are_still_counted(client, db_session, make_website, town):
    now = datetime.now(UTC)
    holder = _source(db_session, make_website, town, "holder", display="Holder")
    other = _source(db_session, make_website, town, "other")
    _run(db_session, holder, now - timedelta(days=2))
    _run(db_session, holder, now - timedelta(hours=1), status="failed")
    _run(db_session, other, now - timedelta(hours=5))
    _run(db_session, other, now - timedelta(hours=1), status="failed")
    text = _text(client.get("/city/bloomington").text)
    assert "Holder could not be checked" in text
    assert "1 other source could not be checked on the latest try" in text


def test_admins_get_a_link_to_the_holding_source(
    client, db_session, make_website, town, make_super_admin, login
):
    now = datetime.now(UTC)
    good = _source(db_session, make_website, town, "good")
    broken = _source(db_session, make_website, town, "broken", display="Broken Calendar")
    _run(db_session, broken, now - timedelta(days=2))
    _run(db_session, broken, now - timedelta(hours=1), status="failed")
    _run(db_session, good, now - timedelta(hours=2))
    make_super_admin(email="root@example.com")
    login("root@example.com")
    page = client.get("/city/bloomington").text
    assert f'<a href="/admin/websites/{broken.id}">Broken Calendar</a>' in page


# --- Admin > Websites -------------------------------------------------------------------


def _admin(client, make_super_admin, login):
    make_super_admin(email="root@example.com")
    login("root@example.com")
    client.get("/admin/websites")


def test_websites_list_shows_freshness_columns(
    client, db_session, make_website, town, make_super_admin, login
):
    now = datetime.now(UTC)
    good = _source(db_session, make_website, town, "good")
    broken = _source(db_session, make_website, town, "broken", schedule=OFF)
    _run(db_session, good, now - timedelta(hours=2))
    _run(db_session, broken, now - timedelta(days=3))
    _run(db_session, broken, now - timedelta(hours=1), status="blocked", error="HTTP 403")
    _admin(client, make_super_admin, login)
    page = client.get("/admin/websites").text
    for heading in ("Last successful check", "Next check", "Latest failure", "Daily check"):
        assert f"<th>{heading}</th>" in page
    text = _text(page)
    assert "Blocked" in text and "HTTP 403" in text
    assert "Automatic imports off" in text
    assert "Overdue" in text
    assert "Holds back town time" in text


def test_needs_attention_filter_keeps_only_sources_to_look_at(
    client, db_session, make_website, town, make_super_admin, login
):
    now = datetime.now(UTC)
    good = _source(db_session, make_website, town, "good-source")
    _run(db_session, good, now - timedelta(hours=2))
    _source(db_session, make_website, town, "switched-off", schedule=OFF)
    _source(db_session, make_website, town, "blocked-source", status=BLOCKED)
    _admin(client, make_super_admin, login)
    page = client.get("/admin/websites?freshness=attention").text
    assert "switched-off" in page
    assert "good-source" not in page and "blocked-source" not in page
    assert 'name="freshness" value="attention" checked' in page


# --- Daily-check baseline -------------------------------------------------------------


def test_plan_sorts_sources_without_changing_anything(db_session, make_website, town):
    met = _source(db_session, make_website, town, "met")
    missing = _source(db_session, make_website, town, "missing", schedule=None)
    off = _source(db_session, make_website, town, "off", schedule=OFF)
    slow = _source(db_session, make_website, town, "slow",
                   schedule={"enabled": True, "interval_minutes": 4320})
    failing = _source(db_session, make_website, town, "failing", schedule=OFF)
    _run(db_session, failing, NOW - timedelta(days=20))
    _run(db_session, failing, NOW - timedelta(days=19), status="failed")
    _source(db_session, make_website, town, "blocked", status=BLOCKED)
    _source(db_session, make_website, town, "archived", status=ARCHIVED)

    plan = plan_daily_checks(db_session, now=NOW)
    ids = lambda items: [item.website.id for item in items]  # noqa: E731
    assert ids(plan.meets) == [met.id]
    assert ids(plan.missing) == [missing.id]
    assert ids(plan.off) == [off.id]
    assert ids(plan.slow) == [slow.id]
    assert ids(plan.held) == [failing.id]
    assert plan.not_scheduled == 2
    assert plan.should_be_checked == 5
    db_session.refresh(off)
    assert off.schedule_config == OFF


def test_apply_turns_on_only_picked_sources_and_audits(db_session, make_website, town):
    off_a = _source(db_session, make_website, town, "off-a",
                    schedule={**OFF, "max_retries": 4, "retry_backoff_seconds": 120})
    off_b = _source(db_session, make_website, town, "off-b", schedule=OFF)
    missing = _source(db_session, make_website, town, "missing", schedule=None)
    approved_before = json.dumps(off_a.approved_pattern, sort_keys=True)

    result = apply_daily_checks(
        db_session, website_ids=[off_a.id, missing.id], actor_id=None, now=NOW
    )
    assert {site.id for site in result.changed} == {off_a.id, missing.id}
    for site in (off_a, off_b, missing):
        db_session.refresh(site)
    assert off_a.schedule_config["enabled"] is True
    assert off_a.schedule_config["interval_minutes"] == 1440
    # Retry and backoff settings are kept.
    assert (off_a.schedule_config["max_retries"], off_a.schedule_config["retry_backoff_seconds"]) \
        == (4, 120)
    assert missing.schedule_config["enabled"] is True
    assert off_b.schedule_config == OFF  # not picked, not changed
    assert json.dumps(off_a.approved_pattern, sort_keys=True) == approved_before

    # First checks are spread out, not all due at once, and none is immediate.
    times = sorted(
        get_or_create_state(db_session, site.id).next_run_at.replace(tzinfo=UTC)
        for site in (off_a, missing)
    )
    assert times[0] >= NOW + timedelta(minutes=5)
    assert times[1] - times[0] == timedelta(hours=12)
    assert not get_or_create_state(db_session, off_a.id).paused

    entries = db_session.query(AuditLog).filter(AuditLog.action == "schedule_daily_baseline")
    assert {entry.entity_id for entry in entries} == {off_a.id, missing.id}


def test_apply_never_schedules_failing_or_ineligible_sources(db_session, make_website, town):
    failing = _source(db_session, make_website, town, "failing", schedule=OFF)
    _run(db_session, failing, NOW - timedelta(days=20))
    _run(db_session, failing, NOW - timedelta(days=19), status="failed")
    blocked = _source(db_session, make_website, town, "blocked", status=BLOCKED, schedule=OFF)
    archived = _source(db_session, make_website, town, "archived", status=ARCHIVED, schedule=OFF)

    result = apply_daily_checks(
        db_session, website_ids=[failing.id, blocked.id, archived.id], actor_id=None, now=NOW
    )
    assert result.changed == []
    assert {site.id for site in result.skipped} == {failing.id, blocked.id, archived.id}
    for site in (failing, blocked, archived):
        db_session.refresh(site)
        assert site.schedule_config == OFF


def test_daily_checks_page_and_form(
    client, db_session, make_website, town, make_super_admin, login
):
    off = _source(db_session, make_website, town, "switched-off", schedule=OFF)
    met = _source(db_session, make_website, town, "met")
    _run(db_session, met, datetime.now(UTC) - timedelta(hours=1))
    _admin(client, make_super_admin, login)
    page = client.get("/admin/websites/daily-checks")
    assert page.status_code == 200
    text = _text(page.text)
    assert "1 of 2 sources are checked daily or more often" in text
    assert "Automatic imports switched off (1)" in text
    assert f'name="website_id" value="{off.id}" checked' in page.text

    resp = client.post(
        "/admin/websites/daily-checks",
        data={"csrf_token": client.cookies.get("csrf_token"), "website_id": str(off.id)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.refresh(off)
    assert off.schedule_config["enabled"] is True


def test_daily_checks_form_needs_csrf(
    client, db_session, make_website, town, make_super_admin, login
):
    off = _source(db_session, make_website, town, "switched-off", schedule=OFF)
    _admin(client, make_super_admin, login)
    resp = client.post(
        "/admin/websites/daily-checks", data={"csrf_token": "wrong", "website_id": str(off.id)},
        follow_redirects=False,
    )
    assert resp.status_code in (400, 403)
    db_session.refresh(off)
    assert off.schedule_config == OFF
