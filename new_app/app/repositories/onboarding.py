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

from app.core.onboarding import ARCHIVED
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


# --- How a submitted URL is matched to an existing Website ------------------
#
# Match type: which rule fired, most specific first.
MATCH_LISTING_URL = "listing_url"
MATCH_BASE_URL = "base_url"
MATCH_PAGE_UNDER_ORIGIN = "page_under_origin"

_MATCH_TYPE_RANK: dict[str, int] = {
    MATCH_LISTING_URL: 0,
    MATCH_BASE_URL: 1,
    MATCH_PAGE_UNDER_ORIGIN: 2,
}

_MATCH_TYPE_DESCRIPTION: dict[str, str] = {
    MATCH_LISTING_URL: "matched the website's event listing URL",
    MATCH_BASE_URL: "matched the website's base URL",
    MATCH_PAGE_UNDER_ORIGIN: "the submitted page belongs to this website's base URL",
}

# Reason: the lifecycle-level answer, which is what a caller branches on.
# An archived match is still a match — it is never silently skipped — but it
# is labelled distinctly so the caller can require a human decision instead of
# treating it as an ordinary duplicate.
REASON_EXISTING = "existing"
REASON_ARCHIVED_EXISTING = "archived_existing"

# Lifecycle preference. A live source outranks a dormant one, and any
# non-archived row outranks an archived one.
_LIFECYCLE_ACTIVE = 0
_LIFECYCLE_ONBOARDING = 1
_LIFECYCLE_ARCHIVED = 2


@dataclass(frozen=True)
class WebsiteMatch:
    website: Website
    # Which rule fired (MATCH_* above) — for audit and UI detail.
    match_type: str
    # Lifecycle answer (REASON_* above) — what callers branch on.
    reason: str
    is_archived: bool
    # (lifecycle rank, match-type rank, website id). Lower sorts first; also
    # exposed so a caller or test can assert *why* one match beat another.
    priority: tuple[int, int, int]

    @property
    def description(self) -> str:
        text = _MATCH_TYPE_DESCRIPTION[self.match_type]
        return f"{text} (archived)" if self.is_archived else text


def _lifecycle_rank(website: Website) -> int:
    if website.archived_at is not None or website.onboarding_status == ARCHIVED:
        return _LIFECYCLE_ARCHIVED
    return _LIFECYCLE_ACTIVE if website.is_active else _LIFECYCLE_ONBOARDING


def _match_type_for(website: Website, url: str, target_origin: str) -> str | None:
    """Which rule, if any, connects `url` to `website`.

    Rule 3 (page-under-origin) deliberately does not apply when the stored row
    already has a listing URL: two different event paths on one host (a
    theatre's and a music school's, say) are two different sources, and
    equating them would be exactly the "arbitrary different paths" match this
    must avoid.
    """
    if website.event_listing_url and same_resource(website.event_listing_url, url):
        return MATCH_LISTING_URL
    if same_resource(website.base_url, url):
        return MATCH_BASE_URL
    if (
        website.event_listing_url is None
        and is_origin_only(website.base_url)
        and canonical_url(website.base_url) == target_origin
    ):
        return MATCH_PAGE_UNDER_ORIGIN
    return None


def find_website_matches(db: Session, url: str) -> list[WebsiteMatch]:
    """Every Website `url` could belong to, best first.

    Ranking, in order:

    1. **lifecycle** — active, then non-archived-but-not-live, then archived.
       Row order must never decide which of two rows for one source wins; an
       archived row cannot shadow the live one that replaced it.
    2. **match specificity** — an exact listing-URL match beats an exact
       base-URL match, which beats a page-under-origin match.
    3. **lowest website id**, as the final tiebreaker. Chosen over "most
       recently updated" precisely because it cannot change: the earliest row
       is the one existing events, provenance and extraction runs are most
       likely to already reference, and a matcher that returns a different
       answer after an unrelated edit would be a worse problem than the one
       this ranking fixes.

    Both sides of every comparison go through `canonical_url`, so trailing
    slash, host casing, default port and fragment differences never split one
    source into two rows.
    """
    if not canonical_url(url):
        return []
    target_origin = canonical_origin(url)

    matches: list[WebsiteMatch] = []
    for website in db.query(Website).order_by(Website.id).all():
        match_type = _match_type_for(website, url, target_origin)
        if match_type is None:
            continue
        lifecycle = _lifecycle_rank(website)
        is_archived = lifecycle == _LIFECYCLE_ARCHIVED
        matches.append(
            WebsiteMatch(
                website=website,
                match_type=match_type,
                reason=REASON_ARCHIVED_EXISTING if is_archived else REASON_EXISTING,
                is_archived=is_archived,
                priority=(lifecycle, _MATCH_TYPE_RANK[match_type], website.id),
            )
        )
    matches.sort(key=lambda match: match.priority)
    return matches


def find_website_match(db: Session, url: str) -> WebsiteMatch | None:
    """The single best match for `url`, or None. See `find_website_matches`
    for the ranking."""
    matches = find_website_matches(db, url)
    return matches[0] if matches else None


def find_website_by_url(db: Session, url: str) -> Website | None:
    match = find_website_match(db, url)
    return match.website if match else None
