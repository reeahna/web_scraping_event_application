"""Manual "import all active websites" orchestration.

Every website is imported through the SAME `run_extraction` path used by the
scheduled and one-site manual imports — so approved-config version, HTTP/browser
strategy, normalization, dedup, persistence and ExtractionRun history are all
identical. This module only plans the set, bounds concurrency, honours the
per-site scheduler lock, isolates failures, and records per-website outcomes.

It never changes any website's `next_run_at`: it releases the per-site lock with
`schedule=None`, so a bulk import leaves each site's automatic cadence exactly
where it was (a manual import, not a reschedule).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.database import SessionLocal
from app.models.bulk_import import (
    BULK_COMPLETED,
    BULK_COMPLETED_WITH_FAILURES,
    BULK_QUEUED,
    BULK_RUNNING,
    ITEM_BLOCKED,
    ITEM_FAILED,
    ITEM_PARTIAL,
    ITEM_QUEUED,
    ITEM_RUNNING,
    ITEM_SKIPPED_ALREADY_RUNNING,
    ITEM_SKIPPED_INELIGIBLE,
    ITEM_SUCCESS,
    BulkImportItem,
    BulkImportRun,
)
from app.models.website import Website
from app.services.scheduler import (
    MAX_CONCURRENT_RUNS,
    evaluate_manual_import_eligibility,
    release_lock,
    try_acquire,
)

logger = get_logger("bulk_import")

ExtractionFn = Callable[..., Awaitable[object]]

_PROBLEM_ITEM_STATUSES = frozenset({ITEM_FAILED, ITEM_BLOCKED, ITEM_PARTIAL})
_STATUS_MAP = {"success": ITEM_SUCCESS, "partial": ITEM_PARTIAL, "blocked": ITEM_BLOCKED}


# --- planning (for the confirmation summary) ---------------------------------


@dataclass
class BulkImportPlan:
    eligible: list[Website] = field(default_factory=list)
    ineligible: list[tuple[Website, list[str]]] = field(default_factory=list)

    @property
    def eligible_count(self) -> int:
        return len(self.eligible)

    @property
    def http_count(self) -> int:
        return sum(
            1
            for w in self.eligible
            if evaluate_manual_import_eligibility(w).execution_strategy != "browser"
        )

    @property
    def browser_count(self) -> int:
        return sum(
            1
            for w in self.eligible
            if evaluate_manual_import_eligibility(w).execution_strategy == "browser"
        )

    @property
    def skipped_count(self) -> int:
        return len(self.ineligible)


def plan_bulk_import(db: Session) -> BulkImportPlan:
    """Split every website into "importable now" vs "skipped", using MANUAL
    import eligibility (an active, approved source of an active city) — NOT
    whether automatic scheduling is enabled, so a deliberately disabled schedule
    is still included in a one-off manual bulk import."""
    plan = BulkImportPlan()
    for website in db.scalars(select(Website).order_by(Website.name)):
        eligibility = evaluate_manual_import_eligibility(website)
        if eligibility.eligible:
            plan.eligible.append(website)
        else:
            plan.ineligible.append((website, list(eligibility.reasons)))
    return plan


def active_bulk_run(db: Session) -> BulkImportRun | None:
    """A bulk run that is still queued or running, if any — the guard against
    starting a second, duplicate bulk operation."""
    return db.scalar(
        select(BulkImportRun)
        .where(BulkImportRun.status.in_((BULK_QUEUED, BULK_RUNNING)))
        .order_by(BulkImportRun.id.desc())
    )


def create_bulk_import(db: Session, *, requested_by_user_id: int | None) -> BulkImportRun:
    """Create a queued bulk run with one item per currently-eligible website.
    The dedicated scheduler process (or a direct call to `run_bulk_import`)
    executes it. Refuses to create a second run while one is unfinished."""
    plan = plan_bulk_import(db)
    run = BulkImportRun(
        requested_by_user_id=requested_by_user_id,
        status=BULK_QUEUED,
        eligible_count=plan.eligible_count,
        http_count=plan.http_count,
        browser_count=plan.browser_count,
        skipped_count=plan.skipped_count,
    )
    db.add(run)
    db.flush()
    for website in plan.eligible:
        strategy = evaluate_manual_import_eligibility(website).execution_strategy
        db.add(
            BulkImportItem(
                bulk_run_id=run.id, website_id=website.id, website_name=website.name,
                execution_strategy=strategy, status=ITEM_QUEUED,
            )
        )
    db.commit()
    db.refresh(run)
    return run


# --- execution ---------------------------------------------------------------


async def _default_extraction_fn(db: Session, website: Website):
    from app.services.extraction_runs import run_extraction

    return await run_extraction(
        db, website, triggered_by_user_id=None,
        correlation_id=f"bulk:{datetime.now(UTC).isoformat()}",
    )


def _map_item_status(run_status: str) -> str:
    return _STATUS_MAP.get(run_status, ITEM_FAILED)


async def _run_item(
    item_id: int, *, session_factory, holder: str, extraction_fn: ExtractionFn
) -> None:
    db = session_factory()
    try:
        item = db.get(BulkImportItem, item_id)
        website = db.get(Website, item.website_id) if item else None
        if item is None or website is None:
            return

        eligibility = evaluate_manual_import_eligibility(website)
        if not eligibility.eligible:
            item.status = ITEM_SKIPPED_INELIGIBLE
            item.error_summary = "; ".join(eligibility.reasons)[:500]
            item.completed_at = datetime.now(UTC)
            db.commit()
            return

        # Honour the SAME per-site lock the scheduler uses: skip a site already
        # running (scheduled / manual / another bulk) rather than double-run it.
        state = try_acquire(db, website.id, holder)
        if state is None:
            item.status = ITEM_SKIPPED_ALREADY_RUNNING
            item.completed_at = datetime.now(UTC)
            db.commit()
            return

        item.status = ITEM_RUNNING
        item.started_at = datetime.now(UTC)
        db.commit()
        try:
            result = await extraction_fn(db, website)
            run_status = getattr(result, "status", "failed")
            item.status = _map_item_status(run_status)
            item.events_found = getattr(result, "events_found", 0)
            item.events_valid = getattr(result, "events_valid", 0)
            item.events_inserted = getattr(result, "events_inserted", 0)
            item.events_updated = getattr(result, "events_updated", 0)
            item.duplicates_skipped = getattr(result, "duplicates_skipped", 0)
            item.extraction_run_id = getattr(result, "run_id", None)
        except Exception as exc:  # noqa: BLE001 - one site's failure must not stop the rest
            logger.warning("bulk import website %s failed: %s", website.id, exc)
            item.status = ITEM_FAILED
            item.error_summary = f"{type(exc).__name__}: {exc}"[:500]
        finally:
            item.completed_at = datetime.now(UTC)
            db.commit()
            # schedule=None -> the per-site lock is released but next_run_at is
            # LEFT UNCHANGED: a bulk import never reschedules a site.
            release_lock(db, state, status=item.status, schedule=None)
    finally:
        db.close()


def _finalize_status(item_statuses: list[str]) -> str:
    if any(s in _PROBLEM_ITEM_STATUSES for s in item_statuses):
        return BULK_COMPLETED_WITH_FAILURES
    return BULK_COMPLETED


async def run_bulk_import(
    bulk_run_id: int,
    *,
    session_factory=SessionLocal,
    extraction_fn: ExtractionFn | None = None,
    holder: str | None = None,
    max_concurrent: int = MAX_CONCURRENT_RUNS,
) -> BulkImportRun | None:
    """Execute a queued bulk run: at most `max_concurrent` websites at once, each
    through `run_extraction`, failures isolated, per-site lock honoured, no
    next_run_at changes. Idempotent — only a still-queued run is executed."""
    extraction_fn = extraction_fn or _default_extraction_fn
    holder = holder or f"bulk:{bulk_run_id}"

    db = session_factory()
    try:
        run = db.get(BulkImportRun, bulk_run_id)
        if run is None or run.status != BULK_QUEUED:
            return run  # already running/finished elsewhere — do not double-run
        run.status = BULK_RUNNING
        run.started_at = datetime.now(UTC)
        item_ids = [item.id for item in run.items]
        db.commit()
    finally:
        db.close()

    semaphore = asyncio.Semaphore(max(1, max_concurrent))

    async def _guarded(item_id: int) -> None:
        async with semaphore:
            await _run_item(
                item_id, session_factory=session_factory, holder=holder, extraction_fn=extraction_fn
            )

    await asyncio.gather(*(_guarded(i) for i in item_ids), return_exceptions=True)

    db = session_factory()
    try:
        run = db.get(BulkImportRun, bulk_run_id)
        if run is None:
            return None
        statuses = [item.status for item in run.items]
        run.status = _finalize_status(statuses)
        run.completed_at = datetime.now(UTC)
        db.commit()
        db.refresh(run)
        return run
    finally:
        db.close()


async def drain_bulk_import_queue(session_factory=SessionLocal) -> int:
    """Run any queued bulk operations to completion. Called from the dedicated
    scheduler process tick, so a long browser-heavy bulk import runs in that
    isolated process with the shared concurrency ceiling, never in a web
    request. Returns how many bulk runs were executed."""
    db = session_factory()
    try:
        queued_ids = list(
            db.scalars(
                select(BulkImportRun.id).where(BulkImportRun.status == BULK_QUEUED)
            ).all()
        )
    finally:
        db.close()
    for run_id in queued_ids:
        await run_bulk_import(run_id, session_factory=session_factory)
    return len(queued_ids)
