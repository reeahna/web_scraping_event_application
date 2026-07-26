"""Phase 10: the scheduled-run runner (lock, retries, cancel, re-onboarding).

A fake extraction is injected, so nothing here touches the network or the real
scheduler. State is read back through a fresh view of the shared engine.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core.onboarding import ACTIVE
from app.services import scheduler as sched
from app.services.scheduler_runner import run_scheduled_extraction

_CONFIG = {"configuration": {"pattern_name": "json_ld_event", "listing_url": "https://e.org/x"}}


async def _noop_sleep(_seconds):
    return None


def _result(status: str, events: int = 5):
    return SimpleNamespace(status=status, events_found=events)


@pytest.fixture
def eligible_website(make_city, make_website, db_session):
    city = make_city(is_active=True)
    website = make_website(city, approved_pattern=_CONFIG, is_active=True)
    website.onboarding_status = ACTIVE
    website.schedule_config = {"enabled": True, "interval_minutes": 60, "max_retries": 1}
    db_session.commit()
    db_session.refresh(website)
    return website


def _reload_state(db_session, website_id):
    db_session.expire_all()
    return sched.get_or_create_state(db_session, website_id)


def test_a_successful_run_releases_the_lock_and_schedules_next(eligible_website, db_session):
    async def fake(db, website):
        return _result("success", events=7)

    outcome = asyncio.run(
        run_scheduled_extraction(
            eligible_website.id, holder="h1", extraction_fn=fake, sleep_fn=_noop_sleep
        )
    )
    assert outcome.ran is True
    assert outcome.status == "success"
    assert outcome.attempts == 1

    state = _reload_state(db_session, eligible_website.id)
    assert state.running is False
    assert state.last_run_status == "success"
    assert state.next_run_at is not None
    assert state.consecutive_structure_failures == 0


def test_failures_retry_up_to_the_limit_and_count_as_structural(eligible_website, db_session):
    calls = {"n": 0}

    async def fake(db, website):
        calls["n"] += 1
        return _result("failed", events=0)

    outcome = asyncio.run(
        run_scheduled_extraction(
            eligible_website.id, holder="h1", extraction_fn=fake, sleep_fn=_noop_sleep,
            reonboard_fn=_fake_reonboard(),
        )
    )
    # max_retries=1 -> one initial attempt plus one retry.
    assert outcome.attempts == 2
    assert calls["n"] == 2
    assert outcome.status == "failed"
    state = _reload_state(db_session, eligible_website.id)
    assert state.consecutive_structure_failures == 1


def test_retry_then_success(eligible_website, db_session):
    calls = {"n": 0}

    async def fake(db, website):
        calls["n"] += 1
        return _result("failed", 0) if calls["n"] == 1 else _result("success", 4)

    outcome = asyncio.run(
        run_scheduled_extraction(
            eligible_website.id, holder="h1", extraction_fn=fake, sleep_fn=_noop_sleep
        )
    )
    assert outcome.attempts == 2
    assert outcome.status == "success"
    assert _reload_state(db_session, eligible_website.id).consecutive_structure_failures == 0


def test_a_locked_site_is_skipped(eligible_website, db_session):
    # Another holder already owns a fresh lock.
    sched.try_acquire(db_session, eligible_website.id, "other-holder")

    async def fake(db, website):  # should never be called
        raise AssertionError("extraction ran despite an active lock")

    outcome = asyncio.run(
        run_scheduled_extraction(
            eligible_website.id, holder="h1", extraction_fn=fake, sleep_fn=_noop_sleep
        )
    )
    assert outcome.ran is False
    assert outcome.status == "skipped_locked"


def test_reonboarding_triggers_at_the_structure_failure_threshold(eligible_website, db_session):
    state = sched.get_or_create_state(db_session, eligible_website.id)
    state.consecutive_structure_failures = sched.STRUCTURE_FAILURE_THRESHOLD - 1
    db_session.commit()

    triggered = {"called": False}

    async def fake(db, website):
        return _result("failed", 0)

    async def fake_reonboard(db, website):
        triggered["called"] = True
        return True

    outcome = asyncio.run(
        run_scheduled_extraction(
            eligible_website.id, holder="h1", extraction_fn=fake, sleep_fn=_noop_sleep,
            reonboard_fn=fake_reonboard,
        )
    )
    assert outcome.reonboarding_triggered is True
    assert triggered["called"] is True
    # Counter resets so it fires once per threshold crossing, not every run.
    assert _reload_state(db_session, eligible_website.id).consecutive_structure_failures == 0


def test_an_ineligible_site_is_paused_not_run(make_city, make_website, db_session):
    city = make_city(is_active=True)
    website = make_website(city, name="Draft")  # draft, not schedulable

    async def fake(db, website):
        raise AssertionError("ineligible site should not run")

    outcome = asyncio.run(
        run_scheduled_extraction(
            website.id, holder="h1", extraction_fn=fake, sleep_fn=_noop_sleep
        )
    )
    assert outcome.ran is False
    assert outcome.status == "ineligible"
    assert _reload_state(db_session, website.id).paused is True


def _fake_reonboard():
    async def _f(db, website):
        return True

    return _f
