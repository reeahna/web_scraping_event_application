"""Runs one scheduled extraction under the durable per-site lock (Phase 10).

Separated from ``app.services.scheduler`` (the pure decision logic + state
store) and from ``app.scheduler`` (the APScheduler process) so the runner can
be unit-tested against a fake extraction without a live network or a real
scheduler. Automated tests inject a fake ``run_extraction`` — nothing here
makes a live third-party request on its own.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.logging import get_logger
from app.database import SessionLocal
from app.models.website import Website
from app.services.scheduler import (
    STRUCTURE_FAILURE_THRESHOLD,
    evaluate_eligibility,
    heartbeat,
    release_lock,
    set_paused,
    try_acquire,
)

logger = get_logger("scheduler.runner")


@dataclass(frozen=True)
class ScheduledRunOutcome:
    website_id: int
    ran: bool
    status: str
    attempts: int
    reonboarding_triggered: bool = False
    skip_reason: str | None = None


# Injection seams so tests never touch the network or the real scheduler.
ExtractionFn = Callable[..., Awaitable[object]]
ReonboardFn = Callable[..., Awaitable[bool]]


async def run_scheduled_extraction(
    website_id: int,
    *,
    holder: str,
    session_factory=SessionLocal,
    extraction_fn: ExtractionFn | None = None,
    reonboard_fn: ReonboardFn | None = None,
    sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> ScheduledRunOutcome:
    extraction_fn = extraction_fn or _default_extraction_fn
    reonboard_fn = reonboard_fn or _default_reonboard_fn

    db = session_factory()
    try:
        website = db.get(Website, website_id)
        if website is None:
            return ScheduledRunOutcome(website_id, ran=False, status="missing", attempts=0,
                                       skip_reason="website not found")

        eligibility = evaluate_eligibility(website)
        if not eligibility.eligible:
            # No longer schedulable — pause the durable job rather than run it.
            set_paused(db, website_id, True)
            return ScheduledRunOutcome(website_id, ran=False, status="ineligible", attempts=0,
                                       skip_reason="; ".join(eligibility.reasons))

        state = try_acquire(db, website_id, holder)
        if state is None:
            return ScheduledRunOutcome(website_id, ran=False, status="skipped_locked", attempts=0,
                                       skip_reason="already running or paused")

        schedule = eligibility.schedule
        attempts = 0
        result = None
        status = "failed"
        while True:
            db.refresh(state)
            if state.cancel_requested:
                status = "cancelled"
                break
            attempts += 1
            try:
                result = await extraction_fn(db, website)
                status = getattr(result, "status", "failed")
            except Exception as exc:  # noqa: BLE001 - a run failure must not kill the scheduler
                logger.warning("scheduled extraction raised for website %s: %s", website_id, exc)
                result = None
                status = "failed"
            heartbeat(db, state)
            if status in ("success", "partial"):
                break
            if attempts > schedule.max_retries:
                break
            await sleep_fn(schedule.backoff_for_attempt(attempts))

        _update_structure_failures(state, result, status)
        db.commit()
        reonboard = state.consecutive_structure_failures >= STRUCTURE_FAILURE_THRESHOLD
        release_lock(db, state, status=status, schedule=schedule)

        triggered = False
        if reonboard:
            # Refresh detection/draft and notify — never silently replace the
            # approved configuration. Reset the counter so it triggers once per
            # threshold crossing, not on every subsequent run.
            triggered = await reonboard_fn(db, website)
            state.consecutive_structure_failures = 0
            db.commit()

        return ScheduledRunOutcome(
            website_id, ran=True, status=status, attempts=attempts,
            reonboarding_triggered=triggered,
        )
    finally:
        db.close()


def _is_structural_failure(result, status: str) -> bool:
    """A structural failure is one where the *shape* of the source seems to
    have changed — a hard failure, or a run that fetched fine but produced no
    events. A blocked/network failure is transient, not structural."""
    if status == "cancelled":
        return False
    if result is None or status == "failed":
        return True
    if status == "blocked":
        return False
    return getattr(result, "events_found", 0) == 0


def _update_structure_failures(state, result, status: str) -> None:
    if status in ("success", "partial") and getattr(result, "events_found", 0) > 0:
        state.consecutive_structure_failures = 0
    elif _is_structural_failure(result, status):
        state.consecutive_structure_failures += 1


async def _default_extraction_fn(db, website: Website):
    from app.services.extraction_runs import run_extraction

    return await run_extraction(db, website, triggered_by_user_id=None,
                                correlation_id=f"scheduled:{datetime.now(UTC).isoformat()}")


async def _default_reonboard_fn(db, website: Website) -> bool:
    from app.services.scheduler_reonboarding import reonboard_after_structure_failures

    return await reonboard_after_structure_failures(db, website)
