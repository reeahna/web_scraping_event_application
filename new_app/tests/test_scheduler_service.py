"""Phase 10: the durable scheduler service (eligibility, locks, reconcile)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.onboarding import ACTIVE
from app.schemas.schedule import ScheduleConfig
from app.services.scheduler import (
    STALE_LOCK_SECONDS,
    evaluate_eligibility,
    get_or_create_state,
    leader_heartbeat,
    lock_is_stale,
    pause_city_sites,
    reclaim_stale_locks,
    reconcile,
    release_lock,
    scheduler_health,
    set_paused,
    try_acquire,
    try_become_leader,
)

_CONFIG = {"configuration": {"pattern_name": "json_ld_event", "listing_url": "https://e.org/x"}}


def _make_eligible(make_city, make_website, db_session, **city_kw):
    city = make_city(is_active=city_kw.get("city_active", True))
    website = make_website(city, approved_pattern=_CONFIG, is_active=True)
    website.onboarding_status = ACTIVE
    website.schedule_config = {"enabled": True, "interval_minutes": 60}
    db_session.commit()
    db_session.refresh(website)
    return website


def test_eligible_website_passes(make_city, make_website, db_session):
    website = _make_eligible(make_city, make_website, db_session)
    result = evaluate_eligibility(website)
    assert result.eligible is True
    assert result.schedule is not None


def test_ineligible_reasons_are_reported(make_city, make_website, db_session):
    city = make_city(is_active=False)
    website = make_website(city, is_active=False)  # draft, no approved, inactive city
    result = evaluate_eligibility(website)
    assert result.eligible is False
    joined = "; ".join(result.reasons)
    assert "not active" in joined
    assert "no approved configuration" in joined
    assert "city is not active" in joined
    assert "no schedule configured" in joined


def test_disabled_schedule_is_ineligible(make_city, make_website, db_session):
    website = _make_eligible(make_city, make_website, db_session)
    website.schedule_config = {"enabled": False, "interval_minutes": 60}
    db_session.commit()
    assert evaluate_eligibility(website).eligible is False


def test_per_site_lock_prevents_overlap(make_city, make_website, db_session):
    website = _make_eligible(make_city, make_website, db_session)
    first = try_acquire(db_session, website.id, "holder-A")
    assert first is not None
    second = try_acquire(db_session, website.id, "holder-B")
    assert second is None  # already running, fresh lock


def test_paused_site_cannot_be_acquired(make_city, make_website, db_session):
    website = _make_eligible(make_city, make_website, db_session)
    set_paused(db_session, website.id, True)
    assert try_acquire(db_session, website.id, "holder-A") is None


def test_stale_lock_is_reclaimed(make_city, make_website, db_session):
    website = _make_eligible(make_city, make_website, db_session)
    state = try_acquire(db_session, website.id, "dead-holder")
    # Backdate the heartbeat well past the stale threshold.
    state.lock_heartbeat_at = datetime.now(UTC) - timedelta(seconds=STALE_LOCK_SECONDS + 60)
    db_session.commit()
    assert lock_is_stale(state, datetime.now(UTC), STALE_LOCK_SECONDS) is True
    reclaimed = reclaim_stale_locks(db_session)
    assert website.id in reclaimed
    db_session.refresh(state)
    assert state.running is False
    # And now it can be acquired again.
    assert try_acquire(db_session, website.id, "holder-B") is not None


def test_release_sets_next_run(make_city, make_website, db_session):
    website = _make_eligible(make_city, make_website, db_session)
    state = try_acquire(db_session, website.id, "holder-A")
    release_lock(db_session, state, status="success", schedule=ScheduleConfig(interval_minutes=60))
    db_session.refresh(state)
    assert state.running is False
    assert state.last_run_status == "success"
    assert state.next_run_at is not None


def test_reconcile_pauses_ineligible_and_schedules_eligible(make_city, make_website, db_session):
    eligible = _make_eligible(make_city, make_website, db_session)
    ineligible_city = make_city(name="Other", slug="other")
    ineligible = make_website(ineligible_city, name="Draft site")  # draft, not schedulable

    result = reconcile(db_session)
    assert eligible.id in result.eligible_website_ids
    assert ineligible.id in result.paused_website_ids

    eligible_state = get_or_create_state(db_session, eligible.id)
    assert eligible_state.paused is False
    assert eligible_state.next_run_at is not None
    assert get_or_create_state(db_session, ineligible.id).paused is True


def test_pause_city_sites(make_city, make_website, db_session):
    city = make_city()
    a = make_website(city, name="A")
    b = make_website(city, name="B")
    count = pause_city_sites(db_session, city.id, True)
    assert count == 2
    assert get_or_create_state(db_session, a.id).paused is True
    assert get_or_create_state(db_session, b.id).paused is True


def test_leader_election_and_health(db_session):
    assert try_become_leader(db_session, "proc-1") is True
    # A different holder cannot steal a fresh lease.
    assert try_become_leader(db_session, "proc-2") is False
    assert leader_heartbeat(db_session, "proc-1") is True
    assert leader_heartbeat(db_session, "proc-2") is False

    health = scheduler_health(db_session)
    assert health.leader_holder == "proc-1"
    assert health.leader_is_fresh is True
