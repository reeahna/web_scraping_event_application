"""APScheduler-backed scheduler runtime.

Two recurring ticks:

* ``_heartbeat_tick`` — refresh the leader row's heartbeat and reclaim any
  stale per-site locks left by a dead process.
* ``_dispatch_tick`` — if we still hold leadership, find every eligible site
  whose ``next_run_at`` has arrived and is not already running, and launch its
  extraction as a bounded-concurrency task.

Per-site timing and locking live in the database (``scheduler_job_state``), so
this process holds no durable state of its own and a restart simply resumes.
"""

from __future__ import annotations

import asyncio
import os
import socket
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.core.logging import get_logger
from app.database import SessionLocal
from app.models.scheduler import SchedulerJobState
from app.services.scheduler import (
    leader_heartbeat,
    reclaim_stale_locks,
    reconcile,
    try_become_leader,
)
from app.services.scheduler_runner import run_scheduled_extraction

logger = get_logger("scheduler.runtime")

DEFAULT_DISPATCH_INTERVAL_SECONDS = 60
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30
DEFAULT_MAX_CONCURRENT_RUNS = 4


def default_holder() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class SchedulerRuntime:
    def __init__(
        self,
        *,
        holder: str | None = None,
        session_factory=SessionLocal,
        dispatch_interval_seconds: int = DEFAULT_DISPATCH_INTERVAL_SECONDS,
        heartbeat_interval_seconds: int = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        max_concurrent_runs: int = DEFAULT_MAX_CONCURRENT_RUNS,
    ) -> None:
        self.holder = holder or default_holder()
        self._session_factory = session_factory
        self._dispatch_interval = dispatch_interval_seconds
        self._heartbeat_interval = heartbeat_interval_seconds
        self._semaphore = asyncio.Semaphore(max_concurrent_runs)
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._is_leader = False
        self._active_tasks: set[asyncio.Task] = set()
        # Full reconcile runs every N heartbeats so eligibility changes (e.g. a
        # city deactivated through the web app) are picked up without a restart.
        self._heartbeats_between_reconciles = 10
        self._heartbeat_count = 0
        self._geocoder = None  # built in start() when geocoding is enabled

    def start(self) -> None:
        db = self._session_factory()
        try:
            self._is_leader = try_become_leader(db, self.holder)
            if self._is_leader:
                summary = reconcile(db)
                logger.info(
                    "scheduler leader %s: %d eligible, %d paused, %d stale locks reclaimed",
                    self.holder,
                    len(summary.eligible_website_ids),
                    len(summary.paused_website_ids),
                    len(summary.reclaimed_website_ids),
                )
            else:
                logger.info("scheduler %s started as standby (another leader holds the lock)",
                            self.holder)
        finally:
            db.close()

        self._scheduler.add_job(
            self._heartbeat_tick, "interval", seconds=self._heartbeat_interval,
            id="heartbeat", max_instances=1, coalesce=True,
        )
        self._scheduler.add_job(
            self._dispatch_tick, "interval", seconds=self._dispatch_interval,
            id="dispatch", max_instances=1, coalesce=True,
        )
        # The Phase 8C onboarding queue is drained from this same dedicated
        # process rather than from a web worker.
        self._scheduler.add_job(
            self._onboarding_tick, "interval", seconds=self._dispatch_interval,
            id="onboarding", max_instances=1, coalesce=True,
        )
        # Async geocoding (Phase 11) drains from this process too, and only when
        # a provider is actually enabled — the default makes no live call.
        from app.config import get_settings
        from app.services.geocoding import get_geocoder

        settings = get_settings()
        if settings.geocoding_enabled:
            self._geocoder = get_geocoder(settings)
            self._scheduler.add_job(
                self._geocoding_tick, "interval", seconds=self._dispatch_interval,
                id="geocoding", max_instances=1, coalesce=True,
            )
        self._scheduler.start()

    async def _heartbeat_tick(self) -> None:
        db = self._session_factory()
        try:
            # Re-claim leadership if it was vacant/stale, then heartbeat.
            self._is_leader = try_become_leader(db, self.holder)
            if self._is_leader:
                leader_heartbeat(db, self.holder)
                reclaim_stale_locks(db)
                self._heartbeat_count += 1
                if self._heartbeat_count % self._heartbeats_between_reconciles == 0:
                    reconcile(db)
        except Exception as exc:  # noqa: BLE001 - a tick must never kill the loop
            logger.warning("heartbeat tick failed: %s", exc)
        finally:
            db.close()

    def _due_website_ids(self, now: datetime) -> list[int]:
        db = self._session_factory()
        try:
            rows = db.scalars(
                select(SchedulerJobState.website_id).where(
                    SchedulerJobState.paused.is_(False),
                    SchedulerJobState.running.is_(False),
                    SchedulerJobState.next_run_at.is_not(None),
                    SchedulerJobState.next_run_at <= now,
                )
            ).all()
            return list(rows)
        finally:
            db.close()

    async def _dispatch_tick(self) -> None:
        if not self._is_leader:
            return
        try:
            due = self._due_website_ids(datetime.now(UTC))
        except Exception as exc:  # noqa: BLE001
            logger.warning("dispatch query failed: %s", exc)
            return
        for website_id in due:
            task = asyncio.create_task(self._run_one(website_id))
            self._active_tasks.add(task)
            task.add_done_callback(self._active_tasks.discard)

    async def _onboarding_tick(self) -> None:
        if not self._is_leader:
            return
        from app.services.bulk_onboarding import drain_onboarding_queue

        db = self._session_factory()
        try:
            processed = await drain_onboarding_queue(db)
            if processed:
                logger.info("drained %d onboarding job(s)", processed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("onboarding drain failed: %s", exc)
        finally:
            db.close()

    async def _geocoding_tick(self) -> None:
        if not self._is_leader or self._geocoder is None:
            return
        from app.config import get_settings
        from app.services.geocoding import drain_geocoding_queue

        db = self._session_factory()
        try:
            processed = await drain_geocoding_queue(
                db, self._geocoder, limit=get_settings().geocoding_batch_size
            )
            if processed:
                logger.info("geocoded %d event(s)", processed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("geocoding drain failed: %s", exc)
        finally:
            db.close()

    async def _run_one(self, website_id: int) -> None:
        async with self._semaphore:
            try:
                outcome = await run_scheduled_extraction(
                    website_id, holder=self.holder, session_factory=self._session_factory
                )
                logger.info("scheduled run website %s: %s", website_id, outcome.status)
            except Exception as exc:  # noqa: BLE001
                logger.warning("scheduled run website %s crashed: %s", website_id, exc)

    async def shutdown(self) -> None:
        """Graceful: stop scheduling new work, let in-flight runs finish."""
        self._scheduler.shutdown(wait=False)
        if self._active_tasks:
            await asyncio.gather(*self._active_tasks, return_exceptions=True)
        logger.info("scheduler %s shut down", self.holder)
