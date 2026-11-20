"""Admin view of AI event labeling: the queue, events the AI could not label,
and what each recent request to the provider cost.

Retrying only puts events back in the queue; the scheduler process makes the
actual request on its next tick. Nothing here calls the AI provider.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.config import get_settings
from app.core.csrf import verify_csrf
from app.core.exceptions import AppError, NotFoundError
from app.core.flash import set_flash
from app.core.forms import reject_unexpected_form_fields
from app.core.templating import render
from app.dependencies import ClientIp, CorrelationId, DbSession
from app.models.ai_label_batch import AiLabelBatch
from app.models.ai_label_evaluation import AiLabelEvaluation
from app.models.event import Event
from app.models.user import User
from app.services.ai_categorization import (
    needs_review_events,
    queue_counts,
    queue_status,
    retry_needs_review,
    usage_summary,
)
from app.services.ai_label_eval import (
    RUNNING,
    cancel_evaluation,
    progress_summary,
    start_evaluation,
)
from app.services.ai_label_state import reset_for_retry
from app.services.audit import record_audit
from app.services.rbac import require_permission

router = APIRouter(prefix="/admin/ai-labeling", tags=["admin-ai-labeling"])

ViewEvents = Annotated[User, Depends(require_permission("events.view"))]
OverrideCategory = Annotated[User, Depends(require_permission("events.override_category"))]

RECENT_BATCHES = 50
NEEDS_REVIEW_SHOWN = 100


def _overview(db: DbSession) -> dict:
    waiting, labeled = queue_counts(db)
    return {
        "status": queue_status(db),
        "waiting": waiting,
        "labeled": labeled,
        "usage": usage_summary(db),
        "needs_review_total": needs_review_events(db).order_by(None).count(),
    }


@router.get("", response_class=HTMLResponse)
def overview_page(request: Request, current_user: ViewEvents, db: DbSession):
    batches = (
        db.query(AiLabelBatch)
        .order_by(AiLabelBatch.created_at.desc(), AiLabelBatch.id.desc())
        .limit(RECENT_BATCHES)
        .all()
    )
    needs_review = needs_review_events(db).limit(NEEDS_REVIEW_SHOWN).all()
    evaluations = (
        db.query(AiLabelEvaluation).order_by(AiLabelEvaluation.id.desc()).limit(10).all()
    )
    return render(
        request,
        "admin/ai_labeling.html",
        {
            "current_user": current_user,
            "overview": _overview(db),
            "batches": batches,
            "needs_review": needs_review,
            "evaluations": evaluations,
            "candidate_model": get_settings().groq_eval_candidate_model,
        },
    )


@router.get("/status")
def status(db: DbSession, _: ViewEvents) -> JSONResponse:
    """The same numbers as JSON, for monitoring."""
    return JSONResponse(_overview(db))


@router.post("/retry-all")
async def retry_all(
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: OverrideCategory,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    await reject_unexpected_form_fields(request, {"csrf_token"})
    count = retry_needs_review(db)
    db.commit()
    record_audit(
        db,
        actor_id=current_user.id,
        action="ai_labels_retried",
        entity_type="event",
        after={"events_requeued": count},
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse("/admin/ai-labeling", status_code=303)
    set_flash(response, f"{count} event{'s' if count != 1 else ''} sent back for AI labeling.")
    return response


@router.post("/events/{event_id}/retry")
async def retry_event(
    event_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: OverrideCategory,
    csrf_token: str = Form(...),
    next: str = Form("/admin/ai-labeling"),
):
    verify_csrf(request, csrf_token)
    await reject_unexpected_form_fields(request, {"csrf_token", "next"})
    event = db.get(Event, event_id)
    if event is None:
        raise NotFoundError("Event not found")
    before = {"ai_label_status": event.ai_label_status, "ai_attempts": event.ai_attempts}
    reset_for_retry(event)
    db.commit()
    record_audit(
        db,
        actor_id=current_user.id,
        action="ai_label_retried",
        entity_type="event",
        entity_id=event.id,
        before=before,
        after={"ai_label_status": event.ai_label_status, "ai_attempts": 0},
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    # Only ever back to an admin page, never an arbitrary address.
    target = next if next.startswith("/admin/") and not next.startswith("//") else "/admin"
    response = RedirectResponse(target, status_code=303)
    set_flash(response, "Event sent back for AI labeling.")
    return response


# --- Model evaluations -----------------------------------------------------------


def _evaluation_or_404(db: DbSession, evaluation_id: int) -> AiLabelEvaluation:
    evaluation = db.get(AiLabelEvaluation, evaluation_id)
    if evaluation is None:
        raise NotFoundError("Evaluation not found")
    return evaluation


@router.post("/evaluations")
async def start(
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: OverrideCategory,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    await reject_unexpected_form_fields(request, {"csrf_token"})
    try:
        evaluation = start_evaluation(db, user_id=current_user.id)
    except ValueError as exc:
        raise AppError(str(exc), status_code=409) from exc
    db.commit()
    record_audit(
        db,
        actor_id=current_user.id,
        action="ai_label_evaluation_started",
        entity_type="ai_label_evaluation",
        entity_id=evaluation.id,
        after={"events": len(evaluation.sample), "configs": evaluation.configs},
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    response = RedirectResponse(f"/admin/ai-labeling/evaluations/{evaluation.id}", 303)
    set_flash(response, "Evaluation started. It runs in the background, one request a minute.")
    return response


@router.post("/evaluations/{evaluation_id}/cancel")
async def cancel(
    evaluation_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: OverrideCategory,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    await reject_unexpected_form_fields(request, {"csrf_token"})
    evaluation = _evaluation_or_404(db, evaluation_id)
    if evaluation.status != RUNNING:
        raise AppError("Only a running evaluation can be stopped", status_code=409)
    cancel_evaluation(evaluation)
    db.commit()
    record_audit(
        db,
        actor_id=current_user.id,
        action="ai_label_evaluation_cancelled",
        entity_type="ai_label_evaluation",
        entity_id=evaluation.id,
        correlation_id=correlation_id,
        ip_address=ip_address,
    )
    return RedirectResponse(f"/admin/ai-labeling/evaluations/{evaluation.id}", 303)


def _evaluation_view(evaluation: AiLabelEvaluation) -> dict:
    return {
        "id": evaluation.id,
        "status": evaluation.status,
        "created_at": evaluation.created_at.isoformat() if evaluation.created_at else None,
        "finished_at": evaluation.finished_at.isoformat() if evaluation.finished_at else None,
        "requests_made": evaluation.requests_made,
        "configs": evaluation.configs,
        "progress": progress_summary(evaluation),
        "error": evaluation.error,
        "report": evaluation.report,
    }


@router.get("/evaluations/{evaluation_id}", response_class=HTMLResponse)
def evaluation_page(
    evaluation_id: int, request: Request, current_user: ViewEvents, db: DbSession
):
    evaluation = _evaluation_or_404(db, evaluation_id)
    return render(
        request,
        "admin/ai_label_evaluation.html",
        {"current_user": current_user, "evaluation": _evaluation_view(evaluation)},
    )


@router.get("/evaluations/{evaluation_id}/data")
def evaluation_json(evaluation_id: int, db: DbSession, _: ViewEvents) -> JSONResponse:
    return JSONResponse(_evaluation_view(_evaluation_or_404(db, evaluation_id)))
