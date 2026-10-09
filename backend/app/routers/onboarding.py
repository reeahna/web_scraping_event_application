"""Bulk source onboarding routes.

Registered *before* the websites router in app.main so `/admin/websites/
onboard` is matched here rather than by that router's `/{website_id}` path
parameter.

Every action is permission-checked by dependency, not by hiding a button:
viewing needs sites.view, submitting needs sites.create, processing/retrying
needs sites.test, cancelling needs sites.update.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import get_settings
from app.core.csrf import verify_csrf
from app.core.exceptions import AppError, NotFoundError
from app.core.flash import set_flash
from app.core.onboarding_jobs import (
    READY_FOR_APPROVAL,
    RETRYABLE_STATUSES,
    SOURCE_CSV,
    SOURCE_PASTE,
    SOURCE_SINGLE,
)
from app.core.templating import render
from app.core.timezones import is_fixed_offset_abbreviation
from app.dependencies import ClientIp, CorrelationId, DbSession
from app.models.user import User
from app.repositories.auto_onboarding import (
    decision_for_job,
    get_policy,
    list_policies,
    newest_decision_ids_for_jobs,
)
from app.repositories.city import list_cities
from app.repositories.onboarding import (
    get_batch,
    get_job,
    list_batches,
    list_jobs_for_batch,
    status_counts,
)
from app.schemas.city import _VALID_TIMEZONES
from app.services.audit import record_audit
from app.services.auto_onboarding_execution import effective_decision
from app.services.bulk_onboarding import (
    cancel_job,
    create_batch_from_submission,
    process_batch,
    refresh_batch_progress,
    retry_job,
)
from app.services.onboarding_submission import (
    ParsedSubmission,
    SubmissionLimits,
    parse_csv,
    parse_url_lines,
)
from app.services.rbac import require_permission, user_has_permission

router = APIRouter(tags=["admin-onboarding"])

ViewSites = Annotated[User, Depends(require_permission("sites.view"))]
CreateSites = Annotated[User, Depends(require_permission("sites.create"))]
TestSites = Annotated[User, Depends(require_permission("sites.test"))]
UpdateSites = Annotated[User, Depends(require_permission("sites.update"))]

PER_PAGE = 20
MAX_CSV_READ_BYTES = 8_000_000  # hard read ceiling; the real cap is in Settings


def _limits() -> SubmissionLimits:
    settings = get_settings()
    return SubmissionLimits(
        max_urls=settings.onboarding_max_urls_per_batch,
        max_csv_rows=settings.onboarding_max_csv_rows,
        max_csv_bytes=settings.onboarding_max_csv_bytes,
        max_url_length=settings.onboarding_max_url_length,
    )


def _submission_context(request: Request, current_user: User, db, **extra) -> dict:
    settings = get_settings()
    # Only a settings.manage user may pick a policy at submission time (and
    # only such a policy could enable approval anyway); everyone else gets
    # the ordinary city -> global default resolution with no selector shown.
    can_select_policy = user_has_permission(db, current_user, "settings.manage")
    return {
        "current_user": current_user,
        "cities": list_cities(db, active_only=False),
        "timezones": sorted(_VALID_TIMEZONES),
        "can_select_policy": can_select_policy,
        "policies": list_policies(db, active_only=True) if can_select_policy else [],
        "limits": {
            "urls": settings.onboarding_max_urls_per_batch,
            "csv_rows": settings.onboarding_max_csv_rows,
            "csv_bytes": settings.onboarding_max_csv_bytes,
            "url_length": settings.onboarding_max_url_length,
        },
        "errors": {},
        "parsed": None,
        **extra,
    }


# --- Submission -------------------------------------------------------------


@router.get("/admin/websites/onboard", response_class=HTMLResponse)
def onboard_form(request: Request, current_user: CreateSites, db: DbSession):
    return render(
        request, "admin/onboarding/submit.html", _submission_context(request, current_user, db)
    )


@router.post("/admin/websites/onboard", response_class=HTMLResponse)
async def onboard_submit(
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    ip_address: ClientIp,
    current_user: CreateSites,
    urls: str = Form(""),
    city_id: str = Form(""),
    default_timezone: str = Form(""),
    redetect_existing: str | None = Form(None),
    policy_id: str = Form(""),
    csv_file: UploadFile | None = File(None),  # noqa: B008
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    limits = _limits()

    upload_bytes = b""
    if csv_file is not None and csv_file.filename:
        upload_bytes = await csv_file.read(MAX_CSV_READ_BYTES + 1)

    if upload_bytes:
        parsed: ParsedSubmission = parse_csv(upload_bytes, limits)
        source_kind = SOURCE_CSV
    else:
        parsed = parse_url_lines(urls, limits)
        source_kind = SOURCE_SINGLE if len(parsed.rows) == 1 else SOURCE_PASTE

    errors: dict[str, str] = {}
    if parsed.error:
        errors["submission"] = parsed.error
    elif not parsed.rows:
        errors["submission"] = "No valid URLs were submitted."

    selected_city_id = int(city_id) if city_id else None
    if selected_city_id is None and not errors:
        errors["city_id"] = "Choose a default city — sources cannot be approved without one."
    if default_timezone and default_timezone not in _VALID_TIMEZONES:
        errors["default_timezone"] = f"'{default_timezone}' is not a recognized IANA timezone."
    elif default_timezone and is_fixed_offset_abbreviation(default_timezone):
        # A bare abbreviation like EST ignores daylight saving time and would
        # override the assigned city's correct zone. Leaving this blank uses the
        # city's timezone, which is the right default.
        errors["default_timezone"] = (
            f"'{default_timezone}' is a fixed-offset abbreviation that ignores daylight "
            "saving time. Leave this blank to use the city's timezone, or enter a full IANA "
            "name such as 'America/Indiana/Indianapolis'."
        )

    # A batch-level policy override is only honoured when it exists and — if it
    # enables automatic approval or activation — the submitter holds
    # settings.manage. Selecting a permissive policy must never be a way for a
    # submit-only user to grant themselves approval power.
    selected_policy_id = int(policy_id) if policy_id else None
    if selected_policy_id is not None and not errors:
        policy = get_policy(db, selected_policy_id)
        if policy is None:
            errors["policy_id"] = "The selected onboarding policy no longer exists."
        elif (
            policy.automatic_approval_enabled or policy.automatic_activation_enabled
        ) and not user_has_permission(db, current_user, "settings.manage"):
            errors["policy_id"] = (
                "Selecting a policy that enables automatic approval or activation requires "
                "the settings.manage permission."
            )

    if errors:
        return render(
            request,
            "admin/onboarding/submit.html",
            _submission_context(
                request,
                current_user,
                db,
                errors=errors,
                parsed=parsed,
                form={"urls": urls, "city_id": city_id, "default_timezone": default_timezone},
            ),
            status_code=422,
        )

    batch = create_batch_from_submission(
        db,
        parsed,
        submitted_by_user_id=current_user.id,
        default_city_id=selected_city_id,
        default_timezone=default_timezone or None,
        redetect_existing=redetect_existing is not None,
        source_kind=source_kind,
        selected_policy_id=selected_policy_id,
        correlation_id=correlation_id,
    )
    record_audit(
        db,
        actor_id=current_user.id,
        action="onboarding_batch_submitted",
        entity_type="onboarding_batch",
        entity_id=batch.id,
        after={"valid": batch.valid_count, "invalid": batch.invalid_count},
        correlation_id=correlation_id,
        ip_address=ip_address,
    )

    progress = await process_batch(
        db, batch, limit=get_settings().onboarding_jobs_per_request, actor_id=current_user.id
    )
    response = RedirectResponse(url=f"/admin/onboarding/batches/{batch.id}", status_code=303)
    message = (
        f"Queued {batch.valid_count} source(s); processed {progress.processed}. "
        f"{progress.remaining} remaining."
    )
    if batch.invalid_count:
        message += f" {batch.invalid_count} row(s) were rejected."
    set_flash(response, message, "success" if batch.valid_count else "error")
    return response


# --- Batches ----------------------------------------------------------------


@router.get("/admin/onboarding/batches", response_class=HTMLResponse)
def batch_list(request: Request, current_user: ViewSites, db: DbSession, page: int = 1):
    batches, total = list_batches(db, page=page, per_page=PER_PAGE)
    return render(
        request,
        "admin/onboarding/batches.html",
        {
            "current_user": current_user,
            "batches": batches,
            "counts_by_batch": {batch.id: status_counts(db, batch.id) for batch in batches},
            "total": total,
            "page": page,
            "per_page": PER_PAGE,
            "can_create": user_has_permission(db, current_user, "sites.create"),
        },
    )


@router.get("/admin/onboarding/batches/{batch_id}", response_class=HTMLResponse)
def batch_detail(batch_id: int, request: Request, current_user: ViewSites, db: DbSession):
    batch = get_batch(db, batch_id)
    if batch is None:
        raise NotFoundError("Onboarding batch not found")
    jobs = list_jobs_for_batch(db, batch.id)
    # Always a dict (empty when there are no jobs or no decisions yet, e.g. an
    # older batch created before decision records existed); one query for all
    # visible jobs rather than one per job. The template uses `.get(job.id)`.
    decision_ids = newest_decision_ids_for_jobs(db, [job.id for job in jobs])
    return render(
        request,
        "admin/onboarding/batch_detail.html",
        {
            "current_user": current_user,
            "batch": batch,
            "jobs": jobs,
            "decision_ids": decision_ids,
            "counts": status_counts(db, batch.id),
            "remaining": sum(1 for job in jobs if job.completed_at is None),
            "ready_status": READY_FOR_APPROVAL,
            "retryable": RETRYABLE_STATUSES,
            "can_process": user_has_permission(db, current_user, "sites.test"),
        },
    )


@router.post("/admin/onboarding/batches/{batch_id}/process")
async def batch_process(
    batch_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    current_user: TestSites,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    batch = get_batch(db, batch_id)
    if batch is None:
        raise NotFoundError("Onboarding batch not found")

    progress = await process_batch(
        db, batch, limit=get_settings().onboarding_jobs_per_request, actor_id=current_user.id
    )
    response = RedirectResponse(url=f"/admin/onboarding/batches/{batch.id}", status_code=303)
    set_flash(
        response,
        f"Processed {progress.processed} source(s); {progress.remaining} remaining.",
        "success",
    )
    return response


# --- Jobs -------------------------------------------------------------------


@router.get("/admin/onboarding/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(job_id: int, request: Request, current_user: ViewSites, db: DbSession):
    job = get_job(db, job_id)
    if job is None:
        raise NotFoundError("Onboarding job not found")
    inference = ((job.website.proposed_pattern or {}) if job.website else {}).get("inference")
    decision = decision_for_job(db, job.id)
    return render(
        request,
        "admin/onboarding/job_detail.html",
        {
            "current_user": current_user,
            "job": job,
            "inference": inference,
            "decision": decision,
            "decision_effective": effective_decision(decision) if decision else None,
            "ready_status": READY_FOR_APPROVAL,
            "retryable": RETRYABLE_STATUSES,
            "can_retry": user_has_permission(db, current_user, "sites.test"),
            "can_cancel": user_has_permission(db, current_user, "sites.update"),
        },
    )


@router.post("/admin/onboarding/jobs/{job_id}/retry")
async def job_retry(
    job_id: int,
    request: Request,
    db: DbSession,
    correlation_id: CorrelationId,
    current_user: TestSites,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    job = get_job(db, job_id)
    if job is None:
        raise NotFoundError("Onboarding job not found")

    await retry_job(db, job, actor_id=current_user.id)
    response = RedirectResponse(url=f"/admin/onboarding/jobs/{job.id}", status_code=303)
    set_flash(response, f"Retried — the job is now '{job.status}'.", "success")
    return response


@router.post("/admin/onboarding/jobs/{job_id}/cancel")
def job_cancel(
    job_id: int,
    request: Request,
    db: DbSession,
    current_user: UpdateSites,
    csrf_token: str = Form(...),
):
    verify_csrf(request, csrf_token)
    job = get_job(db, job_id)
    if job is None:
        raise NotFoundError("Onboarding job not found")
    if job.batch is None:
        raise AppError("This job is not part of a batch.", status_code=409)

    cancel_job(db, job, actor_id=current_user.id)
    refresh_batch_progress(db, job.batch, actor_id=current_user.id)
    response = RedirectResponse(url=f"/admin/onboarding/jobs/{job.id}", status_code=303)
    set_flash(response, "Onboarding job cancelled.", "success")
    return response
