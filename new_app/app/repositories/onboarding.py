"""Persistence for onboarding batches and jobs.

Duplicate lookups all compare against `normalize_url` output, never a raw
string, so "https://Example.org/events/" and "http://example.org/events" are
recognized as the same source.
"""

from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.onboarding_jobs import ACTIVE_STATUSES, BATCH_OPEN
from app.models.onboarding_batch import OnboardingBatch
from app.models.onboarding_job import OnboardingJob
from app.models.website import Website
from app.services.fingerprints import normalize_url


def create_batch(
    db: Session,
    *,
    submitted_by_user_id: int | None,
    default_city_id: int | None,
    default_timezone: str | None,
    redetect_existing: bool,
    source_kind: str,
    correlation_id: str | None = None,
) -> OnboardingBatch:
    batch = OnboardingBatch(
        submitted_by_user_id=submitted_by_user_id,
        default_city_id=default_city_id,
        default_timezone=default_timezone,
        redetect_existing=redetect_existing,
        source_kind=source_kind,
        status=BATCH_OPEN,
        correlation_id=correlation_id,
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


def create_job(db: Session, **values) -> OnboardingJob:
    job = OnboardingJob(**values)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def get_batch(db: Session, batch_id: int) -> OnboardingBatch | None:
    return db.get(OnboardingBatch, batch_id)


def get_job(db: Session, job_id: int) -> OnboardingJob | None:
    return db.get(OnboardingJob, job_id)


def list_batches(
    db: Session, *, page: int = 1, per_page: int = 20
) -> tuple[list[OnboardingBatch], int]:
    query = db.query(OnboardingBatch)
    total = query.count()
    page = max(page, 1)
    items = (
        query.order_by(OnboardingBatch.created_at.desc(), OnboardingBatch.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
        .all()
    )
    return items, total


def list_jobs_for_batch(db: Session, batch_id: int) -> list[OnboardingJob]:
    return (
        db.query(OnboardingJob)
        .filter(OnboardingJob.batch_id == batch_id)
        .order_by(OnboardingJob.row_number, OnboardingJob.id)
        .all()
    )


def next_queued_jobs(db: Session, batch_id: int, *, limit: int) -> list[OnboardingJob]:
    return (
        db.query(OnboardingJob)
        .filter(
            OnboardingJob.batch_id == batch_id,
            OnboardingJob.status.in_(tuple(ACTIVE_STATUSES)),
        )
        .order_by(OnboardingJob.row_number, OnboardingJob.id)
        .limit(limit)
        .all()
    )


def status_counts(db: Session, batch_id: int) -> dict[str, int]:
    """Per-outcome counts, derived rather than stored — see OnboardingBatch."""
    rows = (
        db.query(OnboardingJob.status, func.count(OnboardingJob.id))
        .filter(OnboardingJob.batch_id == batch_id)
        .group_by(OnboardingJob.status)
        .all()
    )
    return {status: count for status, count in rows}


def find_active_job_for_url(
    db: Session, normalized_url: str, *, exclude_job_id: int | None = None
) -> OnboardingJob | None:
    """An in-flight job for the same URL — the "don't queue the same source
    twice" check."""
    query = db.query(OnboardingJob).filter(
        OnboardingJob.normalized_url == normalized_url,
        OnboardingJob.status.in_(tuple(ACTIVE_STATUSES)),
    )
    if exclude_job_id is not None:
        query = query.filter(OnboardingJob.id != exclude_job_id)
    return query.order_by(OnboardingJob.id).first()


def find_website_by_url(db: Session, url: str) -> Website | None:
    """Matches an existing Website on either its base URL or its event listing
    URL, compared in normalized form. Websites predate normalization, so this
    normalizes both sides in Python rather than relying on a stored column."""
    target = normalize_url(url)
    if not target:
        return None
    for website in db.query(Website).order_by(Website.id).all():
        if normalize_url(website.base_url) == target:
            return website
        if website.event_listing_url and normalize_url(website.event_listing_url) == target:
            return website
    return None
