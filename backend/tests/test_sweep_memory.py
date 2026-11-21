"""The daily clean-up sweeps read events in bounded batches and take turns.

Before this, the home-town and attendability sweeps each loaded every active
event at once, and both started at the same moment after every deploy. With a
large events table that pushed the 512 MB production instance (web process and
scheduler together) over its memory limit. These tests pin the three
properties the fix relies on: every eligible event is still checked exactly
once, the number of events held at a time is bounded by the batch size, and
the two sweeps never run at the same time.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from app.models.event import Event
from app.scheduler.runtime import SchedulerRuntime
from app.services import attendability, event_sweeps, home_town
from app.services.event_sweeps import active_event_batches


def _active(db):
    return db.query(Event).filter(Event.is_active.is_(True), Event.archived_at.is_(None))


def _events_in_session(db) -> int:
    return sum(isinstance(obj, Event) for obj in db.identity_map.values())


@pytest.fixture
def town_events(db_session, make_city, make_event):
    """23 active events plus 3 that no sweep may touch (inactive or archived),
    interleaved so batch boundaries fall between them."""
    city = make_city(name="Bloomington", slug="bloomington", state_or_region="Indiana")
    eligible: list[int] = []
    for i in range(26):
        if i == 3:
            make_event(city, title="Inactive", canonical_url="https://x/inactive",
                       is_active=False)
        elif i in (11, 19):
            make_event(city, title=f"Archived {i}", canonical_url=f"https://x/archived-{i}",
                       archived=True)
        else:
            event = make_event(city, title=f"Event {i}", canonical_url=f"https://x/{i}")
            eligible.append(event.id)
    db_session.expunge_all()
    return eligible


def test_batches_cover_every_active_event_once(db_session, town_events):
    seen: list[int] = []
    sizes: list[int] = []
    for batch in active_event_batches(db_session, batch_size=4):
        sizes.append(len(batch))
        seen.extend(event.id for event in batch)
    assert seen == town_events  # each once, in id order, none of the skipped ones
    assert max(sizes) == 4
    assert len(sizes) == 6  # 23 events in batches of 4


def test_batches_survive_deactivating_and_committing_between_batches(db_session, town_events):
    """A sweep that hides events and commits each batch must not skip the
    events after them (keyset paging on id, not offsets)."""
    seen: list[int] = []
    for batch in active_event_batches(db_session, batch_size=5):
        for event in batch:
            seen.append(event.id)
            event.is_active = False
        db_session.commit()
    assert seen == town_events
    assert _active(db_session).count() == 0


def test_batch_size_must_be_positive(db_session):
    with pytest.raises(ValueError):
        next(active_event_batches(db_session, batch_size=0))


def test_home_town_sweep_checks_every_event_with_bounded_memory(
    db_session, town_events, monkeypatch
):
    monkeypatch.setattr(event_sweeps, "SWEEP_BATCH_SIZE", 4)
    checked: list[int] = []
    held: list[int] = []
    hide = set(town_events[::5])

    def outside(event) -> bool:
        checked.append(event.id)
        held.append(_events_in_session(db_session))
        return event.id in hide

    monkeypatch.setattr(home_town, "event_outside_home_town", outside)
    found = home_town.hide_out_of_town_events(db_session, apply=True)

    assert checked == town_events
    assert sorted(e.id for e in found) == sorted(hide)
    # At most one batch, plus the events already found (kept for the caller).
    assert max(held) <= 4 + len(hide)
    db_session.expire_all()
    still_active = {e.id for e in _active(db_session)}
    assert still_active == set(town_events) - hide


def test_attendability_sweep_checks_every_event_with_bounded_memory(
    db_session, town_events, monkeypatch
):
    monkeypatch.setattr(event_sweeps, "SWEEP_BATCH_SIZE", 4)
    checked: list[int] = []
    held: list[int] = []
    hide = set(town_events[1::6])

    def reason(event) -> str | None:
        checked.append(event.id)
        held.append(_events_in_session(db_session))
        return "online_only" if event.id in hide else None

    monkeypatch.setattr(attendability, "event_not_attendable_reason", reason)
    found = attendability.hide_unattendable_events(db_session, apply=True)

    assert checked == town_events
    assert sorted(e.id for e, _ in found) == sorted(hide)
    assert {r for _, r in found} == {"online_only"}
    assert max(held) <= 4 + len(hide)
    db_session.expire_all()
    still_active = {e.id for e in _active(db_session)}
    assert still_active == set(town_events) - hide


def test_dry_run_changes_nothing(db_session, town_events, monkeypatch):
    monkeypatch.setattr(event_sweeps, "SWEEP_BATCH_SIZE", 4)
    monkeypatch.setattr(home_town, "event_outside_home_town", lambda event: True)
    found = home_town.hide_out_of_town_events(db_session, apply=False)
    assert [e.id for e in found] == town_events
    # Found events stay readable for a dry-run report.
    assert all(e.title.startswith("Event ") for e in found)
    db_session.rollback()
    active = _active(db_session).count()
    assert active == len(town_events)


@pytest.mark.asyncio
async def test_the_two_sweeps_never_overlap(monkeypatch):
    """Both ticks fire together (as on a deploy); the second waits for the first."""
    running = 0
    peak = 0
    ran: list[str] = []
    lock = threading.Lock()

    def slow(name):
        def sweep(db, *, apply):
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            time.sleep(0.05)
            with lock:
                running -= 1
            ran.append(name)
            return []
        return sweep

    monkeypatch.setattr(home_town, "hide_out_of_town_events", slow("home_town"))
    monkeypatch.setattr(attendability, "hide_unattendable_events", slow("attendability"))

    runtime = SchedulerRuntime(holder="test", session_factory=lambda: _NullSession())
    runtime._is_leader = True
    await asyncio.gather(runtime._home_town_sweep_tick(), runtime._attendability_sweep_tick())

    assert sorted(ran) == ["attendability", "home_town"]
    assert peak == 1


class _NullSession:
    def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_takeover_staggers_the_sweeps():
    runtime = SchedulerRuntime(holder="test")
    later = datetime.now(UTC) + timedelta(hours=24)

    async def noop() -> None:
        return None

    for job_id in ("home_town_sweep", "attendability_sweep", "college_towns"):
        runtime._scheduler.add_job(noop, "interval", hours=24, id=job_id, next_run_time=later)
    runtime._scheduler.start(paused=True)
    try:
        before = datetime.now(UTC)
        runtime._run_on_start_jobs_now()
        jobs = {j: runtime._scheduler.get_job(j).next_run_time for j in
                ("home_town_sweep", "attendability_sweep", "college_towns")}
        assert jobs["college_towns"] <= before + timedelta(seconds=5)
        assert jobs["home_town_sweep"] >= before + timedelta(minutes=2)
        assert jobs["attendability_sweep"] >= jobs["home_town_sweep"] + timedelta(minutes=5)
        assert jobs["attendability_sweep"] <= before + timedelta(minutes=8)
    finally:
        runtime._scheduler.shutdown(wait=False)
