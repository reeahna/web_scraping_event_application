"""BulkOnboardingService: submitted URLs -> reviewable onboarding results.

Per job the sequence is: validate -> resolve redirects -> duplicate checks ->
locate or create a Website -> hand off to the Phase 8B
`detect_and_configure` -> copy its references and metrics onto the job ->
land on a terminal status.

Two boundaries are deliberate:

* **Phase 8B is not reimplemented here.** Detection, proposal, draft save,
  preview, quality scoring, unsupported-site reports and their notifications
  all happen inside `detect_and_configure`; this service only records what
  came back. That is also why no unsupported report is created here — doing
  so would double-report every unsupported source.
* **Website state is never written directly.** Creation goes through
  `repositories.website.create_website` (which starts every site inactive and
  in DRAFT) and any status change through the transition service.

Transaction boundaries: each job is committed on its own. A job that raises
rolls back only its own partial work (`db.rollback()`), is marked `failed`,
and the loop continues — one bad source can never undo the jobs that already
succeeded in the same batch. The batch counters are updated after each job,
so an interrupted run leaves accurate progress behind.

Processing model: synchronous and bounded (`onboarding_jobs_per_request`).
`process_job` takes a single job and no request context, so a Phase 10
background worker can call it unchanged. Nothing here claims durability: a
process that dies mid-job leaves that job in a processing status, which
`retry_job` is the recovery path for.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.onboarding_jobs import (
    BATCH_COMPLETED,
    BLOCKED,
    CANCELLED,
    CONFIGURING,
    CREATING_WEBSITE,
    DETECTING,
    DUPLICATE,
    FAILED,
    LOCATING_EXISTING,
    NEEDS_REVIEW,
    PREVIEWING,
    QUEUED,
    READY_FOR_APPROVAL,
    RETRYABLE_STATUSES,
    UNSUPPORTED,
    VALIDATING,
    is_terminal,
)
from app.extraction.inference import policy as inference_policy
from app.extraction.inference.site_metadata import infer_site_metadata
from app.extraction.types import FetchRequest
from app.models.city import City
from app.models.onboarding_batch import OnboardingBatch
from app.models.onboarding_job import OnboardingJob
from app.models.website import Website
from app.repositories.city import get_city_by_slug
from app.repositories.onboarding import (
    create_batch,
    create_job,
    find_active_job_for_url,
    find_website_by_url,
    list_jobs_for_batch,
    next_queued_jobs,
    status_counts,
)
from app.repositories.website import create_website
from app.schemas.extraction import FetchConfig
from app.schemas.website import WebsiteCreate
from app.services import extraction_runs
from app.services.audit import record_audit
from app.services.fingerprints import normalize_url
from app.services.notifications import (
    SEVERITY_INFO,
    SEVERITY_WARNING,
    build_dedup_fingerprint,
    notify,
)
from app.services.onboarding_automation import detect_and_configure
from app.services.onboarding_submission import ParsedSubmission
from app.services.rbac import users_with_permission

# Phase 8B outcome -> job status. The vocabularies are intentionally the same
# words, but they are different enums owned by different layers, so the
# mapping is explicit rather than implied by a string equality.
_OUTCOME_TO_STATUS: dict[str, str] = {
    inference_policy.READY_FOR_APPROVAL: READY_FOR_APPROVAL,
    inference_policy.NEEDS_REVIEW: NEEDS_REVIEW,
    inference_policy.UNSUPPORTED: UNSUPPORTED,
    inference_policy.BROWSER_REQUIRED: UNSUPPORTED,
    inference_policy.BLOCKED: BLOCKED,
    inference_policy.FAILED: FAILED,
}


@dataclass(frozen=True)
class BatchProgress:
    processed: int
    remaining: int
    counts: dict[str, int]


def _now() -> datetime:
    return datetime.now(UTC)


def create_batch_from_submission(
    db: Session,
    parsed: ParsedSubmission,
    *,
    submitted_by_user_id: int | None,
    default_city_id: int | None,
    default_timezone: str | None,
    redetect_existing: bool,
    source_kind: str,
    correlation_id: str | None = None,
) -> OnboardingBatch:
    """Turns a parsed submission into a batch plus one queued job per valid
    row. Rows rejected during parsing are recorded on the batch rather than
    as jobs — a row with no usable URL has nothing to process — so the invalid
    count is still visible and auditable."""
    batch = create_batch(
        db,
        submitted_by_user_id=submitted_by_user_id,
        default_city_id=default_city_id,
        default_timezone=default_timezone,
        redetect_existing=redetect_existing,
        source_kind=source_kind,
        correlation_id=correlation_id,
    )

    rejected = [row.as_dict() for row in parsed.rejected]
    for row in parsed.rows:
        city_id = default_city_id
        if row.city_slug:
            city = get_city_by_slug(db, row.city_slug)
            if city is None:
                rejected.append(
                    {
                        "row": row.row_number,
                        "value": row.url,
                        "reason": f"unknown city slug '{row.city_slug}'",
                    }
                )
                continue
            city_id = city.id
        create_job(
            db,
            batch_id=batch.id,
            row_number=row.row_number,
            submitted_url=row.url,
            normalized_url=row.normalized_url,
            city_id=city_id,
            # Precedence: the row's own timezone, then the submission default.
            # A city timezone is not copied here — it is resolved at
            # extraction time from the assigned city, so leaving this null
            # means "use the city's".
            timezone_override=row.timezone or default_timezone,
            submitted_by_user_id=submitted_by_user_id,
            submitted_name=row.name,
            submitted_source_display_name=row.source_display_name,
            correlation_id=correlation_id,
        )

    jobs = list_jobs_for_batch(db, batch.id)
    batch.submitted_count = parsed.submitted_count
    batch.valid_count = len(jobs)
    batch.invalid_count = len(rejected)
    batch.rejected_rows = rejected or None
    db.commit()
    db.refresh(batch)

    record_audit(
        db,
        actor_id=submitted_by_user_id,
        action="onboarding_batch_created",
        entity_type="onboarding_batch",
        entity_id=batch.id,
        after={
            "source_kind": source_kind,
            "submitted": batch.submitted_count,
            "valid": batch.valid_count,
            "invalid": batch.invalid_count,
            "default_city_id": default_city_id,
            "redetect_existing": redetect_existing,
        },
        correlation_id=correlation_id,
    )
    for job in jobs:
        record_audit(
            db,
            actor_id=submitted_by_user_id,
            action="onboarding_job_created",
            entity_type="onboarding_job",
            entity_id=job.id,
            after={"submitted_url": job.submitted_url, "batch_id": batch.id},
            correlation_id=correlation_id,
        )
    return batch


def _fail(job: OnboardingJob, reason: str) -> None:
    job.status = FAILED
    job.failure_reason = reason[:500]
    job.completed_at = _now()


@dataclass(frozen=True)
class _PreflightResult:
    final_url: str | None
    document: str | None
    # Set only for a genuine block (a challenge page, 403/429, or an SSRF
    # refusal) — the states a retry should not sweep up automatically.
    blocked_reason: str | None = None
    # Any other reason the page couldn't be read: 404, 500, wrong content
    # type. An ordinary failure, and retryable.
    failure_reason: str | None = None


async def _resolve_document(url: str) -> _PreflightResult:
    """One bounded pre-flight fetch through the ordinary SSRF-protected
    strategy. It serves two purposes at once — following redirects so the
    *destination* can be duplicate-checked before a Website is created, and
    supplying the document that site-metadata inference reads. Returns
    (final_url, document, blocked_reason).

    Cost, stated plainly: a brand-new URL is fetched twice, once here and
    once by detection. That is the price of checking a redirect target
    before creating a row, and it is bounded by the same byte cap and
    timeouts as any other fetch.
    """
    fetch_config = FetchConfig()
    response = await extraction_runs.HttpFetchStrategy().fetch(
        FetchRequest(url=url), fetch_config
    )
    if response.blocked_reason is not None:
        return _PreflightResult(
            response.final_url or url, None, blocked_reason=response.blocked_reason
        )
    if response.status_code >= 400:
        return _PreflightResult(
            response.final_url, None, failure_reason=f"HTTP {response.status_code}"
        )
    return _PreflightResult(response.final_url, response.text)


def _resolve_city(db: Session, job: OnboardingJob) -> City | None:
    return db.get(City, job.city_id) if job.city_id is not None else None


def _link_existing(
    db: Session,
    job: OnboardingJob,
    website: Website,
    *,
    actor_id: int | None,
    correlation_id: str | None,
) -> None:
    job.duplicate_of_website_id = website.id
    job.website_id = website.id
    job.status = DUPLICATE
    job.failure_reason = None
    job.completed_at = _now()
    record_audit(
        db,
        actor_id=actor_id,
        action="onboarding_job_duplicate_detected",
        entity_type="onboarding_job",
        entity_id=job.id,
        after={
            "website_id": website.id,
            "website_onboarding_status": website.onboarding_status,
            "normalized_url": job.normalized_url,
        },
        correlation_id=correlation_id,
    )


async def process_job(
    db: Session,
    job: OnboardingJob,
    *,
    redetect_existing: bool = False,
    actor_id: int | None = None,
) -> OnboardingJob:
    """Runs one job to a terminal status. Never raises for an ordinary source
    problem — a bad URL, an unreachable host, a blocked response and an
    unsupported site are all outcomes, not exceptions."""
    correlation_id = job.correlation_id
    job.started_at = job.started_at or _now()
    job.status = VALIDATING
    job.current_step = VALIDATING
    db.commit()

    # --- resolve redirects + fetch the document metadata inference needs ---
    job.current_step = LOCATING_EXISTING
    preflight = await _resolve_document(job.submitted_url)
    final_url, document = preflight.final_url, preflight.document
    job.final_url = final_url
    if preflight.blocked_reason is not None:
        job.status = BLOCKED
        job.failure_reason = f"The source could not be fetched: {preflight.blocked_reason}"
        job.completed_at = _now()
        db.commit()
        return job
    if preflight.failure_reason is not None:
        _fail(job, f"The source could not be fetched: {preflight.failure_reason}")
        db.commit()
        return job

    # --- duplicate checks, in order ---------------------------------------
    existing_open = find_active_job_for_url(db, job.normalized_url, exclude_job_id=job.id)
    if existing_open is not None:
        job.status = DUPLICATE
        job.failure_reason = f"Already queued as onboarding job #{existing_open.id}."
        job.completed_at = _now()
        db.commit()
        return job

    website = find_website_by_url(db, job.submitted_url)
    if website is None and final_url:
        # The redirect destination, checked before anything is created.
        website = find_website_by_url(db, final_url)

    if website is not None and not redetect_existing:
        _link_existing(db, job, website, actor_id=actor_id, correlation_id=correlation_id)
        db.commit()
        return job

    # --- locate or create the Website -------------------------------------
    if website is None:
        job.current_step = CREATING_WEBSITE
        metadata = infer_site_metadata(
            document=document,
            final_url=final_url or job.submitted_url,
            submitted_url=job.submitted_url,
            supplied_name=job.submitted_name,
            supplied_source_display_name=job.submitted_source_display_name,
        )
        job.inferred_metadata = metadata.as_dict()
        try:
            website = create_website(
                db,
                WebsiteCreate(
                    name=metadata.name,
                    source_display_name=metadata.source_display_name,
                    city_id=job.city_id,
                    base_url=metadata.base_url,
                    event_listing_url=metadata.event_listing_url,
                    timezone_override=job.timezone_override,
                ),
            )
        except Exception as exc:  # pydantic validation or DB error
            db.rollback()
            _fail(job, f"The website record could not be created: {exc}")
            db.commit()
            return job
        job.website_id = website.id
        record_audit(
            db,
            actor_id=actor_id,
            action="website_created_from_onboarding",
            entity_type="website",
            entity_id=website.id,
            after={
                "name": website.name,
                "base_url": website.base_url,
                "event_listing_url": website.event_listing_url,
                "city_id": website.city_id,
                "onboarding_job_id": job.id,
                "inferred_fields": sorted(metadata.inferred_fields),
            },
            correlation_id=correlation_id,
        )
    else:
        job.website_id = website.id
        job.duplicate_of_website_id = website.id
        record_audit(
            db,
            actor_id=actor_id,
            action="website_matched_existing",
            entity_type="website",
            entity_id=website.id,
            after={"onboarding_job_id": job.id, "redetect": True},
            correlation_id=correlation_id,
        )

    # --- Phase 8B: the single orchestration path --------------------------
    job.current_step = DETECTING
    db.commit()
    try:
        result = await detect_and_configure(db, website, correlation_id=correlation_id)
    except Exception as exc:
        db.rollback()
        _fail(job, f"Automatic configuration failed: {type(exc).__name__}: {exc}")
        db.commit()
        return job

    job.current_step = PREVIEWING if result.preview is not None else CONFIGURING
    job.detected_pattern = result.inference.pattern_name
    job.detection_confidence = result.inference.detection_confidence
    job.detection_run_id = result.detection.run_id
    db.refresh(website)
    job.configuration_version = website.configuration_version
    if result.preview is not None:
        job.preview_run_id = result.preview.run_id
        job.events_found = result.preview.events_found
        job.events_valid = result.preview.events_valid
        job.events_rejected = result.preview.events_rejected
    if result.quality is not None:
        job.quality = result.quality.as_dict()

    job.status = _OUTCOME_TO_STATUS.get(result.outcome, NEEDS_REVIEW)
    if job.status in (FAILED, BLOCKED, UNSUPPORTED, NEEDS_REVIEW):
        job.failure_reason = (
            result.inference.error
            or ("; ".join(result.blocking_reasons) if result.blocking_reasons else None)
        )
        if job.failure_reason:
            job.failure_reason = job.failure_reason[:500]
    else:
        job.failure_reason = None
    job.completed_at = _now()
    db.commit()

    record_audit(
        db,
        actor_id=actor_id,
        action="onboarding_job_completed" if job.status != FAILED else "onboarding_job_failed",
        entity_type="onboarding_job",
        entity_id=job.id,
        after={
            "status": job.status,
            "website_id": job.website_id,
            "pattern": job.detected_pattern,
            "events_valid": job.events_valid,
            "events_found": job.events_found,
            "preview_run_id": job.preview_run_id,
        },
        correlation_id=correlation_id,
    )
    return job


async def process_batch(
    db: Session,
    batch: OnboardingBatch,
    *,
    limit: int,
    actor_id: int | None = None,
) -> BatchProgress:
    """Processes up to `limit` outstanding jobs. Each job is independent: a
    failure marks that job and the loop moves on."""
    processed = 0
    for job in next_queued_jobs(db, batch.id, limit=limit):
        try:
            await process_job(
                db, job, redetect_existing=batch.redetect_existing, actor_id=actor_id
            )
        except Exception as exc:  # last-resort guard; process_job handles its own
            db.rollback()
            _fail(job, f"Unexpected error: {type(exc).__name__}: {exc}")
            db.commit()
        processed += 1

    refresh_batch_progress(db, batch, actor_id=actor_id)
    remaining = len(next_queued_jobs(db, batch.id, limit=limit + 1))
    return BatchProgress(
        processed=processed, remaining=remaining, counts=status_counts(db, batch.id)
    )


def refresh_batch_progress(
    db: Session,
    batch: OnboardingBatch,
    *,
    actor_id: int | None = None,
    correlation_id: str | None = None,
) -> OnboardingBatch:
    jobs = list_jobs_for_batch(db, batch.id)
    batch.completed_count = sum(1 for job in jobs if is_terminal(job.status))
    was_open = batch.status != BATCH_COMPLETED
    if jobs and batch.completed_count == len(jobs):
        batch.status = BATCH_COMPLETED
        batch.completed_at = batch.completed_at or _now()
    db.commit()
    db.refresh(batch)

    if was_open and batch.status == BATCH_COMPLETED:
        _notify_batch_completed(db, batch, correlation_id=correlation_id or batch.correlation_id)
    return batch


def _notify_batch_completed(
    db: Session, batch: OnboardingBatch, *, correlation_id: str | None
) -> None:
    """One summary per batch rather than one notification per source — a
    50-URL submission must not produce 50 notifications."""
    counts = status_counts(db, batch.id)
    ready = counts.get(READY_FOR_APPROVAL, 0)
    problems = (
        counts.get(FAILED, 0)
        + counts.get(BLOCKED, 0)
        + counts.get(UNSUPPORTED, 0)
        + counts.get(NEEDS_REVIEW, 0)
    )
    recipients = users_with_permission(db, "sites.approve")
    action_url = f"/admin/onboarding/batches/{batch.id}"
    summary = ", ".join(
        f"{count} {status.replace('_', ' ')}" for status, count in sorted(counts.items())
    )

    if ready:
        notify(
            db,
            notification_type="onboarding_batch_completed_ready",
            severity=SEVERITY_INFO,
            title=f"Onboarding batch #{batch.id}: {ready} source(s) ready for approval",
            message=(
                f"Batch #{batch.id} finished with {ready} source(s) ready for approval. "
                f"Outcomes: {summary}."
            ),
            recipients=recipients,
            related_resource_type="onboarding_batch",
            related_resource_id=batch.id,
            action_url=action_url,
            dedup_fingerprint=build_dedup_fingerprint(
                "onboarding_batch_completed_ready", str(batch.id), str(ready)
            ),
            correlation_id=correlation_id,
        )
    if problems:
        notify(
            db,
            notification_type="onboarding_batch_completed_with_failures",
            severity=SEVERITY_WARNING,
            title=f"Onboarding batch #{batch.id}: {problems} source(s) need attention",
            message=(
                f"Batch #{batch.id} finished with {problems} source(s) that could not be "
                f"configured automatically. Outcomes: {summary}."
            ),
            recipients=recipients,
            related_resource_type="onboarding_batch",
            related_resource_id=batch.id,
            action_url=action_url,
            dedup_fingerprint=build_dedup_fingerprint(
                "onboarding_batch_completed_with_failures", str(batch.id), str(problems)
            ),
            correlation_id=correlation_id,
        )


async def retry_job(
    db: Session, job: OnboardingJob, *, actor_id: int | None = None
) -> OnboardingJob:
    """Re-runs a job through the normal workflow. Prior attempt information
    survives in the audit log and in the website's own extraction-run history;
    `retry_count` records how many attempts this job has had.

    A `blocked` job is not retryable here on purpose — retrying a blocked or
    unsafe URL should be a deliberate, separately-reasoned action, not part of
    a generic "retry the failures" sweep.
    """
    from app.core.exceptions import AppError

    if job.status not in RETRYABLE_STATUSES:
        raise AppError(
            f"An onboarding job with status '{job.status}' cannot be retried.", status_code=409
        )

    job.retry_count += 1
    job.status = QUEUED
    job.current_step = None
    job.failure_reason = None
    job.started_at = None
    job.completed_at = None
    db.commit()

    record_audit(
        db,
        actor_id=actor_id,
        action="onboarding_job_retried",
        entity_type="onboarding_job",
        entity_id=job.id,
        after={"retry_count": job.retry_count},
        correlation_id=job.correlation_id,
    )

    batch = job.batch
    redetect = bool(batch.redetect_existing) if batch is not None else False
    # A retry must not create a second Website for a source that already has
    # one: when the job already resolved to a website, re-detection against
    # that website is exactly what should happen.
    await process_job(db, job, redetect_existing=redetect or job.website_id is not None,
                      actor_id=actor_id)
    if batch is not None:
        refresh_batch_progress(db, batch, actor_id=actor_id)
    return job


def cancel_job(db: Session, job: OnboardingJob, *, actor_id: int | None = None) -> OnboardingJob:
    from app.core.exceptions import AppError

    if is_terminal(job.status):
        raise AppError("This onboarding job has already finished.", status_code=409)
    job.status = CANCELLED
    job.current_step = None
    job.completed_at = _now()
    db.commit()
    record_audit(
        db,
        actor_id=actor_id,
        action="onboarding_job_cancelled",
        entity_type="onboarding_job",
        entity_id=job.id,
        correlation_id=job.correlation_id,
    )
    if job.batch is not None:
        refresh_batch_progress(db, job.batch, actor_id=actor_id)
    return job


def normalized(url: str) -> str:
    """Exposed so routes and tests use the same normalization the duplicate
    checks use."""
    return normalize_url(url)
