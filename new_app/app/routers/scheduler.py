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

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.csrf import verify_csrf
from app.core.exceptions import NotFoundError
from app.database import get_db
from app.models.user import User
from app.models.website import Website
from app.services.rbac import require_permission
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
