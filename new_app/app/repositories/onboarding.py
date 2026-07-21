"""Persistence for onboarding batches and jobs.

Every duplicate lookup compares `app.core.url_canonical.canonical_url`
output on both sides — submitted and stored — never a raw string, so
"https://Example.org/events/" and "https://example.org/events" are one
source.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.onboarding_jobs import ACTIVE_STATUSES, BATCH_OPEN
from app.core.url_canonical import (
    canonical_origin,
    canonical_url,
    is_origin_only,
    same_resource,
)
from app.models.onboarding_batch import OnboardingBatch
from app.models.onboarding_job import OnboardingJob
from app.models.website import Website


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


@dataclass(frozen=True)
class WebsiteMatch:
    website: Website
    # Why it matched, so the UI and audit trail can say which rule fired
    # rather than just asserting "duplicate".
    reason: str


def find_website_match(db: Session, url: str) -> WebsiteMatch | None:
    """Locates the existing Website a submitted URL belongs to.

    Both sides go through `canonical_url`, so trailing slash, host casing,
    default port and fragment differences never create a second row. Three
    rules, in order of specificity:

    1. the URL is that website's event listing URL
    2. the URL is that website's base URL
    3. the URL sits under a website whose base URL is the bare origin and
       which has no listing URL of its own — that row represents the whole
       site, so a page within it is the same source

    Rule 3 deliberately does not apply when the stored row already has a
    listing URL: two different event paths on one host (a theatre's and a
    music school's, say) are two different sources, and equating them would
    be exactly the "arbitrary different paths" match this must avoid.
    """
    target = canonical_url(url)
    if not target:
        return None
    target_origin = canonical_origin(url)

    for website in db.query(Website).order_by(Website.id).all():
        if website.event_listing_url and same_resource(website.event_listing_url, url):
            return WebsiteMatch(website, "matched the website's event listing URL")
        if same_resource(website.base_url, url):
            return WebsiteMatch(website, "matched the website's base URL")
        if (
            website.event_listing_url is None
            and is_origin_only(website.base_url)
            and canonical_url(website.base_url) == target_origin
        ):
            return WebsiteMatch(
                website, "the submitted page belongs to this website's base URL"
            )
    return None


def find_website_by_url(db: Session, url: str) -> Website | None:
    match = find_website_match(db, url)
    return match.website if match else None
