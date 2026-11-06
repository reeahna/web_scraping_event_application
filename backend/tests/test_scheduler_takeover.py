"""A standby scheduler that takes over leadership runs the on-start jobs.

On a deploy the new scheduler process starts while the old one still holds a
fresh leader row, so it starts as a standby. The "once on start, then daily"
jobs (college town import, home-town and attendability sweeps, photo refill)
fire immediately, see no leadership and return; without a re-run on takeover
they would wait a full day, so a deploy that adds a college town's file would
not onboard it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models import SchedulerLeader
from app.scheduler.runtime import SchedulerRuntime
from app.services.scheduler import LEADER_ROW_ID, STALE_LEADER_SECONDS


def _leader(db, holder: str, heartbeat_at: datetime) -> None:
    row = db.get(SchedulerLeader, LEADER_ROW_ID)
    if row is None:
        db.add(SchedulerLeader(id=LEADER_ROW_ID, holder=holder, heartbeat_at=heartbeat_at))
    else:
        row.holder = holder
        row.heartbeat_at = heartbeat_at
    db.commit()


@pytest.mark.asyncio
async def test_takeover_reruns_on_start_jobs(db_session):
    _leader(db_session, "old-process", datetime.now(UTC))
    runtime = SchedulerRuntime(holder="new-process")
    calls: list[str] = []
    runtime._run_on_start_jobs_now = lambda: calls.append("now")

    await runtime._heartbeat_tick()  # old leader still fresh: stay standby
    assert runtime._is_leader is False
    assert calls == []

    stale = datetime.now(UTC) - timedelta(seconds=STALE_LEADER_SECONDS + 5)
    _leader(db_session, "old-process", stale)
    await runtime._heartbeat_tick()  # old leader gone: take over
    assert runtime._is_leader is True
    assert calls == ["now"]

    await runtime._heartbeat_tick()  # still leader: nothing to re-run
    assert calls == ["now"]


@pytest.mark.asyncio
async def test_run_on_start_jobs_now_moves_them_up():
    runtime = SchedulerRuntime(holder="test")
    later = datetime.now(UTC) + timedelta(hours=24)

    async def noop() -> None:
        return None

    runtime._scheduler.add_job(noop, "interval", hours=24, id="college_towns",
                               next_run_time=later)
    runtime._scheduler.add_job(noop, "interval", hours=24, id="dispatch",
                               next_run_time=later)
    runtime._scheduler.start(paused=True)
    try:
        runtime._run_on_start_jobs_now()
        assert runtime._scheduler.get_job("college_towns").next_run_time <= (
            datetime.now(UTC) + timedelta(seconds=1)
        )
        # Not an on-start job: left alone. Absent on-start jobs are skipped.
        assert runtime._scheduler.get_job("dispatch").next_run_time == later
    finally:
        runtime._scheduler.shutdown(wait=False)
