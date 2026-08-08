"""Manual "import all active websites" orchestration.

Uses a fake extraction_fn so nothing touches the network; the bulk runner opens
its own sessions on the shared test engine (app.database.SessionLocal), which
conftest points at the isolated test DB.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import pytest

from app.core.onboarding import ACTIVE
from app.models.bulk_import import (
    BULK_COMPLETED,
    BULK_COMPLETED_WITH_FAILURES,
    ITEM_FAILED,
    ITEM_PARTIAL,
    ITEM_SKIPPED_ALREADY_RUNNING,
    ITEM_SUCCESS,
    BulkImportRun,
)
from app.services.bulk_import import (
    active_bulk_run,
    create_bulk_import,
    plan_bulk_import,
    run_bulk_import,
)
from app.services.scheduler import get_or_create_state, try_acquire

pytestmark = pytest.mark.usefixtures("_reset_db")


def _approved(strategy: str = "http") -> dict:
    return {
        "configuration": {
            "pattern_name": "simpleview_events",
            "api_endpoint": "https://example.com/x/find/",
            "execution_strategy": strategy,
            "json_paths": {"events_root": "docs.docs"},
        }
    }


def _site(make_website, city, db_session, *, name, strategy="http", schedule=None,
          is_active=True, archived=False, approved=True):
    w = make_website(
        city, name=name, is_active=is_active, archived=archived,
        approved_pattern=_approved(strategy) if approved else None,
    )
    w.onboarding_status = ACTIVE if (is_active and not archived) else "draft"
    w.schedule_config = schedule
    db_session.commit()
    db_session.refresh(w)
    return w


def _result(status="success", found=10, valid=10, inserted=5, updated=5):
    return NS(
        status=status, events_found=found, events_valid=valid, events_inserted=inserted,
        events_updated=updated, duplicates_skipped=0, run_id=None,
    )


async def _fake_extraction(db, website):
    if "FAIL" in website.name:
        raise RuntimeError("boom")
    if "PARTIAL" in website.name:
        return _result(status="partial", valid=8)
    if "BLOCK" in website.name:
        return _result(status="blocked", found=0, valid=0, inserted=0, updated=0)
    return _result()


def _run(run_id, **kw):
    return asyncio.run(run_bulk_import(run_id, extraction_fn=_fake_extraction, **kw))


# --- planning / eligibility --------------------------------------------------


def test_plan_includes_active_approved_excludes_ineligible(make_city, make_website, db_session):
    city = make_city()
    http = _site(make_website, city, db_session, name="HTTP Site", strategy="http")
    browser = _site(make_website, city, db_session, name="Browser Site", strategy="browser")
    _site(make_website, city, db_session, name="Inactive", is_active=False)
    _site(make_website, city, db_session, name="Archived", archived=True)
    _site(make_website, city, db_session, name="Unapproved", approved=False)

    plan = plan_bulk_import(db_session)
    names = {w.name for w in plan.eligible}
    assert names == {"HTTP Site", "Browser Site"}
    assert plan.http_count == 1
    assert plan.browser_count == 1
    assert plan.skipped_count == 3
    assert http.id and browser.id


def test_disabled_schedule_is_still_bulk_eligible(make_city, make_website, db_session):
    city = make_city()
    _site(make_website, city, db_session, name="Auto Off",
          schedule={"enabled": False, "interval_minutes": 1440})
    plan = plan_bulk_import(db_session)
    # Manual bulk eligibility ignores whether automatic scheduling is enabled.
    assert [w.name for w in plan.eligible] == ["Auto Off"]


def test_inactive_city_site_skipped(make_city, make_website, db_session):
    city = make_city(is_active=False)
    _site(make_website, city, db_session, name="In Inactive City")
    plan = plan_bulk_import(db_session)
    assert plan.eligible == []
    assert plan.skipped_count == 1


# --- create + double-submit guard --------------------------------------------


def test_create_bulk_import_creates_queued_items(make_city, make_website, db_session):
    city = make_city()
    _site(make_website, city, db_session, name="A")
    _site(make_website, city, db_session, name="B", strategy="browser")
    run = create_bulk_import(db_session, requested_by_user_id=None)
    assert run.eligible_count == 2
    assert run.http_count == 1 and run.browser_count == 1
    assert len(run.items) == 2
    assert {i.status for i in run.items} == {"queued"}


def test_active_bulk_run_detects_unfinished(make_city, make_website, db_session):
    city = make_city()
    _site(make_website, city, db_session, name="A")
    run = create_bulk_import(db_session, requested_by_user_id=None)
    assert active_bulk_run(db_session).id == run.id


# --- execution ---------------------------------------------------------------


def test_run_bulk_all_success_is_completed(make_city, make_website, db_session):
    city = make_city()
    for n in ("A", "B", "C"):
        _site(make_website, city, db_session, name=n)
    run = create_bulk_import(db_session, requested_by_user_id=None)
    _run(run.id)
    db_session.expire_all()
    fresh = db_session.get(BulkImportRun, run.id)
    assert fresh.status == BULK_COMPLETED
    assert {i.status for i in fresh.items} == {ITEM_SUCCESS}
    assert all(i.events_found == 10 and i.events_inserted == 5 for i in fresh.items)


def test_one_failure_isolated_others_complete(make_city, make_website, db_session):
    city = make_city()
    _site(make_website, city, db_session, name="Good 1")
    _site(make_website, city, db_session, name="FAIL Site")
    _site(make_website, city, db_session, name="PARTIAL Site")
    run = create_bulk_import(db_session, requested_by_user_id=None)
    _run(run.id)
    db_session.expire_all()
    fresh = db_session.get(BulkImportRun, run.id)
    by_name = {i.website_name: i.status for i in fresh.items}
    assert by_name["Good 1"] == ITEM_SUCCESS
    assert by_name["FAIL Site"] == ITEM_FAILED
    assert by_name["PARTIAL Site"] == ITEM_PARTIAL
    # Mixed outcomes -> completed_with_failures, but every site was processed.
    assert fresh.status == BULK_COMPLETED_WITH_FAILURES
    assert fresh.completed_count == 3


def test_browser_strategy_preserved_in_items(make_city, make_website, db_session):
    city = make_city()
    _site(make_website, city, db_session, name="Browser Site", strategy="browser")
    run = create_bulk_import(db_session, requested_by_user_id=None)
    _run(run.id)
    db_session.expire_all()
    item = db_session.get(BulkImportRun, run.id).items[0]
    assert item.execution_strategy == "browser"
    assert item.status == ITEM_SUCCESS


def test_bulk_does_not_change_next_run_at(make_city, make_website, db_session):
    from datetime import UTC, datetime

    city = make_city()
    w = _site(make_website, city, db_session, name="Scheduled",
              schedule={"enabled": True, "interval_minutes": 1440})
    pinned = datetime(2026, 12, 25, 12, 0, tzinfo=UTC)
    state = get_or_create_state(db_session, w.id)
    state.next_run_at = pinned
    db_session.commit()

    run = create_bulk_import(db_session, requested_by_user_id=None)
    _run(run.id)

    db_session.expire_all()
    after = get_or_create_state(db_session, w.id)
    # A bulk import never reschedules a site (SQLite returns the value tz-naive).
    stored = after.next_run_at
    stored = stored.replace(tzinfo=UTC) if stored.tzinfo is None else stored
    assert stored == pinned
    assert after.running is False


def test_already_running_site_skipped(make_city, make_website, db_session):
    city = make_city()
    w = _site(make_website, city, db_session, name="Busy")
    # Hold the per-site lock as if a scheduled run were in progress.
    assert try_acquire(db_session, w.id, "someone-else") is not None
    run = create_bulk_import(db_session, requested_by_user_id=None)
    _run(run.id)
    db_session.expire_all()
    item = db_session.get(BulkImportRun, run.id).items[0]
    assert item.status == ITEM_SKIPPED_ALREADY_RUNNING


def test_concurrency_is_bounded(make_city, make_website, db_session):
    city = make_city()
    for i in range(8):
        _site(make_website, city, db_session, name=f"Site {i}")
    run = create_bulk_import(db_session, requested_by_user_id=None)

    live = {"current": 0, "peak": 0}

    async def _tracking_extraction(db, website):
        live["current"] += 1
        live["peak"] = max(live["peak"], live["current"])
        await asyncio.sleep(0.02)
        live["current"] -= 1
        return _result()

    asyncio.run(run_bulk_import(run.id, extraction_fn=_tracking_extraction, max_concurrent=3))
    assert live["peak"] <= 3
    db_session.expire_all()
    assert db_session.get(BulkImportRun, run.id).status == BULK_COMPLETED


def test_run_bulk_is_idempotent_for_non_queued(make_city, make_website, db_session):
    city = make_city()
    _site(make_website, city, db_session, name="A")
    run = create_bulk_import(db_session, requested_by_user_id=None)
    _run(run.id)
    db_session.expire_all()
    # A second execution of a completed run does nothing (no double import).
    result = _run(run.id)
    assert result.status == BULK_COMPLETED
