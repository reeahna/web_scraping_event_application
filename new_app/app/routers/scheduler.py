"""Admin scheduler controls (Phase 10).

Manual run-now, cancellation, pause/resume, and a health view. These write
durable state (`scheduler_job_state`, the leader row); the dedicated scheduler
process acts on it. Run-now never runs the extraction inline in the web
request — it schedules the site immediately so the scheduler process (the only
place extraction runs) picks it up on its next dispatch, preserving the "one
place runs jobs, no overlap" guarantee.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.csrf import verify_csrf
from app.core.exceptions import NotFoundError
from app.core.flash import set_flash
from app.core.templating import render
from app.database import get_db
from app.models.bulk_import import BulkImportRun
from app.models.user import User
from app.models.website import Website
from app.services.audit import record_audit
from app.services.bulk_import import active_bulk_run, create_bulk_import, plan_bulk_import
from app.services.rbac import require_permission, user_has_permission
from app.services.schedule_admin import describe_scheduler_process, format_admin_datetime
from app.services.scheduler import (
    evaluate_eligibility,
    get_or_create_state,
    request_cancel,
    scheduler_health,
    set_paused,
)

router = APIRouter(prefix="/admin/scheduler", tags=["admin-scheduler"])

ViewSites = Annotated[User, Depends(require_permission("sites.view"))]
RunSites = Annotated[User, Depends(require_permission("sites.approve"))]

DbSession = Annotated[Session, Depends(get_db)]


# --- admin operations page (HTML) --------------------------------------------


@router.get("", response_class=HTMLResponse)
def operations(request: Request, db: DbSession, current_user: ViewSites):
    plan = plan_bulk_import(db)
    recent = db.scalars(
        select(BulkImportRun).order_by(BulkImportRun.id.desc()).limit(15)
    ).all()
    return render(
        request,
        "admin/scheduler/operations.html",
        {
            "current_user": current_user,
            "scheduler": describe_scheduler_process(db),
            "plan": plan,
            "recent_runs": recent,
            "active_run": active_bulk_run(db),
            "can_run": user_has_permission(db, current_user, "sites.approve"),
            "format_admin_datetime": format_admin_datetime,
        },
    )


@router.post("/bulk-import")
def start_bulk_import(
    request: Request,
    db: DbSession,
    current_user: RunSites,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    existing = active_bulk_run(db)
    if existing is not None:
        # Idempotency guard: never start a second bulk run while one is unfinished.
        response = RedirectResponse(
            url=f"/admin/scheduler/bulk-imports/{existing.id}", status_code=303
        )
        set_flash(response, "A bulk import is already in progress.", "error")
        return response
    run = create_bulk_import(db, requested_by_user_id=current_user.id)
    record_audit(
        db,
        actor_id=current_user.id,
        action="bulk_import_requested",
        entity_type="bulk_import_run",
        entity_id=run.id,
        after={"eligible": run.eligible_count, "skipped": run.skipped_count},
    )
    response = RedirectResponse(url=f"/admin/scheduler/bulk-imports/{run.id}", status_code=303)
    set_flash(response, f"Bulk import #{run.id} queued for {run.eligible_count} website(s).")
    return response


@router.get("/bulk-imports/{run_id}", response_class=HTMLResponse)
def bulk_import_detail(run_id: int, request: Request, db: DbSession, current_user: ViewSites):
    run = db.get(BulkImportRun, run_id)
    if run is None:
        raise NotFoundError("Bulk import not found")
    return render(
        request,
        "admin/scheduler/bulk_detail.html",
        {
            "current_user": current_user,
            "run": run,
            "format_admin_datetime": format_admin_datetime,
        },
    )


def _verify_csrf(request: Request) -> None:
    """Double-submit CSRF for these JSON state-changing POSTs: the token from
    the `X-CSRF-Token` header must match the CSRF cookie."""
    verify_csrf(request, request.headers.get("X-CSRF-Token"))


CsrfChecked = Annotated[None, Depends(_verify_csrf)]


def _website_or_404(db: Session, website_id: int) -> Website:
    website = db.get(Website, website_id)
    if website is None:
        raise NotFoundError("Website not found")
    return website


@router.get("/health")
def health(db: DbSession, _: ViewSites) -> JSONResponse:
    h = scheduler_health(db)
    return JSONResponse(
        {
            "leader_holder": h.leader_holder,
            "leader_is_fresh": h.leader_is_fresh,
            "leader_heartbeat_at": h.leader_heartbeat_at.isoformat()
            if h.leader_heartbeat_at
            else None,
            "scheduled_count": h.scheduled_count,
            "running_count": h.running_count,
            "paused_count": h.paused_count,
        }
    )


@router.post("/websites/{website_id}/queue-now")
def queue_now(
    website_id: int,
    request: Request,
    db: DbSession,
    current_user: RunSites,
    csrf_token: str = Form(...),
):
    """Form-based sibling of run-now for the Website detail page: sets
    next_run_at = now so the scheduler process runs it, then redirects with a
    flash. Distinct from "Run event import now" (which imports inline and never
    touches next_run_at)."""
    verify_csrf(request, csrf_token)
    website = _website_or_404(db, website_id)
    eligibility = evaluate_eligibility(website)
    response = RedirectResponse(url=f"/admin/websites/{website_id}", status_code=303)
    if not eligibility.eligible:
        set_flash(
            response,
            "Cannot queue a scheduled run: " + "; ".join(eligibility.reasons),
            "error",
        )
        return response
    state = get_or_create_state(db, website_id)
    state.paused = False
    state.next_run_at = datetime.now(UTC)
    db.commit()
    set_flash(response, "Queued a scheduled run — the scheduler process will run it shortly.")
    return response


@router.post("/websites/{website_id}/run-now")
def run_now(website_id: int, db: DbSession, _: RunSites, __: CsrfChecked) -> JSONResponse:
    website = _website_or_404(db, website_id)
    eligibility = evaluate_eligibility(website)
    if not eligibility.eligible:
        return JSONResponse(
            {"scheduled": False, "reasons": list(eligibility.reasons)}, status_code=409
        )
    state = get_or_create_state(db, website_id)
    # Schedule immediately; the scheduler process runs it (never inline here).
    state.paused = False
    state.next_run_at = datetime.now(UTC)
    db.commit()
    return JSONResponse({"scheduled": True})


@router.post("/websites/{website_id}/cancel")
def cancel(website_id: int, db: DbSession, _: RunSites, __: CsrfChecked) -> JSONResponse:
    _website_or_404(db, website_id)
    ok = request_cancel(db, website_id)
    return JSONResponse({"cancel_requested": ok})


@router.post("/websites/{website_id}/pause")
def pause(website_id: int, db: DbSession, _: RunSites, __: CsrfChecked) -> JSONResponse:
    _website_or_404(db, website_id)
    set_paused(db, website_id, paused=True)
    return JSONResponse({"paused": True})


@router.post("/websites/{website_id}/resume")
def resume(website_id: int, db: DbSession, _: RunSites, __: CsrfChecked) -> JSONResponse:
    website = _website_or_404(db, website_id)
    eligibility = evaluate_eligibility(website)
    if not eligibility.eligible:
        return JSONResponse(
            {"resumed": False, "reasons": list(eligibility.reasons)}, status_code=409
        )
    set_paused(db, website_id, paused=False)
    return JSONResponse({"resumed": True})
