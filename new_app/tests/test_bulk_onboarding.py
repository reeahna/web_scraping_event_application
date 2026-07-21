"""Bulk onboarding end to end: submission -> queue -> Phase 8B -> outcomes.

Every fetch goes through an httpx.MockTransport (see `patched_http_fetch`),
so the whole production path runs against fixtures with no live network.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.core.onboarding_jobs import (
    BLOCKED,
    DUPLICATE,
    FAILED,
    NEEDS_REVIEW,
    READY_FOR_APPROVAL,
    UNSUPPORTED,
)
from app.models.audit_log import AuditLog
from app.models.event import Event
from app.models.onboarding_job import OnboardingJob
from app.models.website import Website
from app.services.bulk_onboarding import create_batch_from_submission, process_batch, retry_job
from app.services.onboarding_submission import SubmissionLimits, parse_url_lines
from tests.extraction_helpers import load_fixture, patched_http_fetch

GOOD_URL = "https://hall.example.org/events"
SECOND_URL = "https://annex.example.org/events"
UNSUPPORTED_URL = "https://plain.example.org/"
LIMITS = SubmissionLimits(
    max_urls=50, max_csv_rows=50, max_csv_bytes=100_000, max_url_length=2000
)


def _handler(routes: dict[str, str], *, blocked: set[str] | None = None):
    blocked = blocked or set()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in blocked:
            return httpx.Response(403, text="Access Denied")
        body = routes.get(url)
        if body is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    return handler


def _submit(db_session, urls: str, city, *, user=None, redetect=False):
    parsed = parse_url_lines(urls, LIMITS)
    return create_batch_from_submission(
        db_session,
        parsed,
        submitted_by_user_id=user.id if user else None,
        default_city_id=city.id,
        default_timezone=None,
        redetect_existing=redetect,
        source_kind="paste",
        correlation_id="test-correlation",
    )


def _process(db_session, batch, routes, *, blocked=None, limit=10):
    with patched_http_fetch(_handler(routes, blocked=blocked)):
        return asyncio.run(process_batch(db_session, batch, limit=limit))


@pytest.fixture
def city(make_city):
    return make_city(name="Bulk City", slug="bulk-city", timezone="America/Indiana/Indianapolis")


@pytest.fixture
def listing_page():
    return load_fixture("inference_cards_weekday_date.html")


# --- happy path --------------------------------------------------------------


@pytest.fixture
def ready_batch(db_session, city, listing_page, make_super_admin):
    # A recipient for the batch-completion notification must exist, otherwise
    # notify() correctly does nothing.
    make_super_admin(email="bulk-root@example.com")
    batch = _submit(db_session, GOOD_URL, city)
    _process(db_session, batch, {GOOD_URL: listing_page})
    db_session.refresh(batch)
    return batch


def test_a_website_is_created_automatically_with_inferred_metadata(db_session, ready_batch):
    job = ready_batch.jobs[0]
    assert job.website_id is not None
    website = db_session.get(Website, job.website_id)
    assert website.base_url == "https://hall.example.org"
    assert website.event_listing_url == GOOD_URL
    assert website.city_id == ready_batch.default_city_id
    # Created inactive and unapproved, through the normal creation path.
    assert website.is_active is False
    assert website.approved_pattern is None
    assert job.inferred_metadata["inferred_fields"]["base_url"]


def test_detection_configuration_and_preview_all_run(db_session, ready_batch):
    job = ready_batch.jobs[0]
    assert job.status == READY_FOR_APPROVAL
    assert job.detected_pattern == "generic_html_cards"
    assert job.detection_run_id is not None
    assert job.preview_run_id is not None
    assert job.configuration_version >= 1
    assert job.events_found == 5
    assert job.events_valid == 5
    assert job.quality["valid_percentage"] == 1.0


def test_no_events_are_persisted_during_onboarding(db_session, ready_batch):
    assert db_session.query(Event).count() == 0


def test_batch_progress_and_timestamps_are_recorded(db_session, ready_batch):
    assert ready_batch.submitted_count == 1
    assert ready_batch.valid_count == 1
    assert ready_batch.invalid_count == 0
    assert ready_batch.completed_count == 1
    assert ready_batch.status == "completed"
    assert ready_batch.completed_at is not None
    job = ready_batch.jobs[0]
    assert job.started_at is not None and job.completed_at is not None
    assert job.retry_count == 0


def test_audit_records_are_written(db_session, ready_batch):
    actions = {row.action for row in db_session.query(AuditLog).all()}
    assert "onboarding_batch_created" in actions
    assert "onboarding_job_created" in actions
    assert "website_created_from_onboarding" in actions
    assert "onboarding_job_completed" in actions


def test_batch_completion_notifies_once_with_a_summary(db_session, ready_batch):
    from app.models.notification import Notification

    types = [n.notification_type for n in db_session.query(Notification).all()]
    assert types.count("onboarding_batch_completed_ready") <= 1
    assert "onboarding_batch_completed_ready" in types


# --- multiple sources, independent failure ----------------------------------


def test_one_failing_source_does_not_stop_the_batch(db_session, city, listing_page):
    batch = _submit(db_session, f"{GOOD_URL}\n{SECOND_URL}", city)
    # Only the first URL resolves; the second 404s at the pre-flight fetch.
    _process(db_session, batch, {GOOD_URL: listing_page})
    db_session.refresh(batch)

    statuses = {job.submitted_url: job.status for job in batch.jobs}
    assert statuses[GOOD_URL] == READY_FOR_APPROVAL
    assert statuses[SECOND_URL] != READY_FOR_APPROVAL
    # The successful job kept its website and preview.
    good = next(j for j in batch.jobs if j.submitted_url == GOOD_URL)
    assert good.website_id is not None
    assert good.preview_run_id is not None
    assert batch.completed_count == 2


def test_blocked_source_is_classified_as_blocked(db_session, city, listing_page):
    batch = _submit(db_session, f"{GOOD_URL}\n{SECOND_URL}", city)
    _process(db_session, batch, {GOOD_URL: listing_page}, blocked={SECOND_URL})
    db_session.refresh(batch)

    blocked_job = next(j for j in batch.jobs if j.submitted_url == SECOND_URL)
    assert blocked_job.status == BLOCKED
    assert blocked_job.website_id is None  # nothing created for a blocked URL
    assert "could not be fetched" in blocked_job.failure_reason


def test_unsupported_source_is_classified_as_unsupported(db_session, city):
    batch = _submit(db_session, UNSUPPORTED_URL, city)
    _process(db_session, batch, {UNSUPPORTED_URL: load_fixture("unsupported_page.html")})
    db_session.refresh(batch)
    assert batch.jobs[0].status == UNSUPPORTED


def test_needs_review_source_is_classified_as_needs_review(db_session, city):
    url = "https://mixed.example.org/events"
    batch = _submit(db_session, url, city)
    _process(db_session, batch, {url: load_fixture("inference_cards_mixed_quality.html")})
    db_session.refresh(batch)
    job = batch.jobs[0]
    assert job.status == NEEDS_REVIEW
    assert job.failure_reason


# --- idempotency -------------------------------------------------------------


def test_an_existing_base_url_is_matched_instead_of_creating_a_website(
    db_session, city, make_website, listing_page
):
    existing = make_website(city, name="Already Here", base_url="https://hall.example.org")
    before = db_session.query(Website).count()

    batch = _submit(db_session, "https://hall.example.org", city)
    _process(db_session, batch, {"https://hall.example.org": listing_page})
    db_session.refresh(batch)

    job = batch.jobs[0]
    assert job.status == DUPLICATE
    assert job.duplicate_of_website_id == existing.id
    assert db_session.query(Website).count() == before


def test_an_existing_listing_url_is_matched(db_session, city, make_website, listing_page):
    existing = make_website(city, name="Listing Match", base_url="https://other.example.org")
    existing.event_listing_url = GOOD_URL
    db_session.commit()

    batch = _submit(db_session, GOOD_URL, city)
    _process(db_session, batch, {GOOD_URL: listing_page})
    db_session.refresh(batch)
    assert batch.jobs[0].duplicate_of_website_id == existing.id


def test_a_redirect_target_matching_an_existing_website_is_a_duplicate(
    db_session, city, make_website, listing_page
):
    existing = make_website(city, name="Redirect Target", base_url="https://hall.example.org")
    before = db_session.query(Website).count()
    submitted = "https://vanity.example.org/events"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == submitted:
            return httpx.Response(301, headers={"location": "https://hall.example.org"})
        return httpx.Response(200, text=listing_page, headers={"content-type": "text/html"})

    batch = _submit(db_session, submitted, city)
    with patched_http_fetch(handler):
        asyncio.run(process_batch(db_session, batch, limit=5))
    db_session.refresh(batch)

    job = batch.jobs[0]
    assert job.final_url == "https://hall.example.org"
    assert job.status == DUPLICATE
    assert job.duplicate_of_website_id == existing.id
    assert db_session.query(Website).count() == before


def test_an_approved_configuration_is_never_touched_by_a_duplicate_match(
    db_session, city, make_website, listing_page
):
    approved = {"pattern_name": "json_ld_event", "listing_url": GOOD_URL}
    existing = make_website(
        city, name="Approved Site", base_url="https://hall.example.org", approved_pattern=approved
    )
    batch = _submit(db_session, "https://hall.example.org", city)
    _process(db_session, batch, {"https://hall.example.org": listing_page})
    db_session.refresh(existing)

    assert existing.approved_pattern == approved
    assert existing.configuration is None  # detection was not re-run


def test_explicit_redetection_creates_a_draft_and_preserves_the_approved_snapshot(
    db_session, city, make_website, listing_page
):
    approved = {"pattern_name": "json_ld_event", "listing_url": GOOD_URL}
    existing = make_website(
        city, name="Approved Site", base_url="https://hall.example.org", approved_pattern=approved
    )
    batch = _submit(db_session, "https://hall.example.org", city, redetect=True)
    _process(db_session, batch, {"https://hall.example.org": listing_page})
    db_session.refresh(existing)

    assert existing.approved_pattern == approved  # untouched
    assert existing.configuration is not None  # a new draft exists
    assert existing.configuration["pattern_name"] == "generic_html_cards"
    assert batch.jobs[0].status == READY_FOR_APPROVAL


def test_a_second_batch_for_an_in_flight_url_is_marked_duplicate(db_session, city, listing_page):
    first = _submit(db_session, GOOD_URL, city)
    second = _submit(db_session, GOOD_URL, city)
    # Process the second batch while the first job is still queued.
    _process(db_session, second, {GOOD_URL: listing_page})
    db_session.refresh(second)

    job = second.jobs[0]
    assert job.status == DUPLICATE
    assert "Already queued" in job.failure_reason
    assert first.jobs[0].status == "queued"


def test_duplicate_rows_inside_one_submission_never_reach_the_queue(db_session, city):
    batch = _submit(db_session, f"{GOOD_URL}\n{GOOD_URL}/", city)
    assert batch.valid_count == 1
    assert batch.invalid_count == 1
    assert db_session.query(OnboardingJob).filter(OnboardingJob.batch_id == batch.id).count() == 1


# --- retry -------------------------------------------------------------------


def test_retry_reruns_the_job_without_creating_a_second_website(
    db_session, city, listing_page
):
    batch = _submit(db_session, GOOD_URL, city)
    _process(db_session, batch, {})  # nothing served: the job fails
    job = batch.jobs[0]
    assert job.status in (FAILED, UNSUPPORTED, NEEDS_REVIEW)

    before = db_session.query(Website).count()
    with patched_http_fetch(_handler({GOOD_URL: listing_page})):
        asyncio.run(retry_job(db_session, job))
    db_session.refresh(job)

    assert job.retry_count == 1
    assert job.status == READY_FOR_APPROVAL
    assert db_session.query(Website).count() <= before + 1
    actions = {row.action for row in db_session.query(AuditLog).all()}
    assert "onboarding_job_retried" in actions


def test_a_blocked_job_is_not_retryable_without_an_explicit_decision(
    db_session, city, listing_page
):
    from app.core.exceptions import AppError

    batch = _submit(db_session, GOOD_URL, city)
    _process(db_session, batch, {GOOD_URL: listing_page}, blocked={GOOD_URL})
    job = batch.jobs[0]
    assert job.status == BLOCKED

    with pytest.raises(AppError):
        asyncio.run(retry_job(db_session, job))
