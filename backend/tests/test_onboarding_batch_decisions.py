"""Regression: the onboarding batch-detail page renders decisions safely.

Covers the class of failure where a job has no decision (older batch, unsupported
/ failed / needs-review jobs, jobs never evaluated): the page must return 200,
never 500 on a missing `decision_ids[job.id]`.
"""

from __future__ import annotations

import pytest

from app.core.onboarding_jobs import (
    FAILED,
    NEEDS_REVIEW,
    READY_FOR_APPROVAL,
    UNSUPPORTED,
)
from app.models.onboarding_batch import OnboardingBatch
from app.models.onboarding_job import OnboardingJob
from app.repositories.auto_onboarding import (
    create_decision,
    newest_decision_ids_for_jobs,
)

_row = iter(range(1, 100_000))


@pytest.fixture
def admin_client(client, make_super_admin, login):
    make_super_admin(email="batch-root@example.com", password="root-pass-1234")
    login("batch-root@example.com", "root-pass-1234")
    return client


def _batch(db_session, city) -> OnboardingBatch:
    batch = OnboardingBatch(default_city_id=city.id, source_kind="paste", valid_count=0)
    db_session.add(batch)
    db_session.commit()
    db_session.refresh(batch)
    return batch


def _job(db_session, batch, *, status, website=None, url=None) -> OnboardingJob:
    n = next(_row)
    url = url or f"https://source-{n}.example.org/events"
    job = OnboardingJob(
        batch_id=batch.id, row_number=n, submitted_url=url, normalized_url=url,
        status=status, website_id=website.id if website else None,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


def _decision(db_session, *, website, job, final_decision="automatic_approval_denied"):
    return create_decision(
        db_session, website_id=website.id, onboarding_job_id=job.id,
        final_decision=final_decision,
    )


def _get(admin_client, batch):
    return admin_client.get(f"/admin/onboarding/batches/{batch.id}")


def test_batch_detail_with_no_decisions_renders(admin_client, make_city, db_session):
    city = make_city()
    batch = _batch(db_session, city)
    _job(db_session, batch, status=READY_FOR_APPROVAL)
    resp = _get(admin_client, batch)
    assert resp.status_code == 200
    assert "/admin/onboarding/decisions/" not in resp.text


def test_older_batch_without_decision_records_renders(
    admin_client, make_city, make_website, db_session
):
    # Simulates a batch created before decisions existed: terminal jobs, none
    # with a decision row.
    city = make_city()
    website = make_website(city)
    batch = _batch(db_session, city)
    _job(db_session, batch, status=READY_FOR_APPROVAL, website=website)
    _job(db_session, batch, status=FAILED)
    resp = _get(admin_client, batch)
    assert resp.status_code == 200
    assert "/admin/onboarding/decisions/" not in resp.text


def test_batch_detail_with_one_decision_links_it(
    admin_client, make_city, make_website, db_session
):
    city = make_city()
    website = make_website(city)
    batch = _batch(db_session, city)
    job = _job(db_session, batch, status=READY_FOR_APPROVAL, website=website)
    decision = _decision(db_session, website=website, job=job)
    resp = _get(admin_client, batch)
    assert resp.status_code == 200
    assert f"/admin/onboarding/decisions/{decision.id}" in resp.text


def test_mixed_jobs_only_some_have_decisions(
    admin_client, make_city, make_website, db_session
):
    city = make_city()
    website = make_website(city)
    batch = _batch(db_session, city)
    with_decision = _job(db_session, batch, status=READY_FOR_APPROVAL, website=website)
    _job(db_session, batch, status=NEEDS_REVIEW, website=website)  # no decision
    decision = _decision(db_session, website=website, job=with_decision)
    resp = _get(admin_client, batch)
    assert resp.status_code == 200
    # Exactly one decision link — for the job that has one.
    assert resp.text.count("/admin/onboarding/decisions/") == 1
    assert f"/admin/onboarding/decisions/{decision.id}" in resp.text


def test_multiple_decisions_selects_newest(
    admin_client, make_city, make_website, db_session
):
    city = make_city()
    website = make_website(city)
    batch = _batch(db_session, city)
    job = _job(db_session, batch, status=READY_FOR_APPROVAL, website=website)
    older = _decision(db_session, website=website, job=job)
    newer = _decision(db_session, website=website, job=job,
                      final_decision="automatic_approval_allowed")
    assert newer.id > older.id
    resp = _get(admin_client, batch)
    assert resp.status_code == 200
    assert f"/admin/onboarding/decisions/{newer.id}" in resp.text
    assert f"/admin/onboarding/decisions/{older.id}" not in resp.text


@pytest.mark.parametrize("status", [UNSUPPORTED, NEEDS_REVIEW, FAILED, READY_FOR_APPROVAL])
def test_terminal_status_jobs_render(admin_client, make_city, db_session, status):
    city = make_city()
    batch = _batch(db_session, city)
    _job(db_session, batch, status=status)
    resp = _get(admin_client, batch)
    assert resp.status_code == 200  # 200, never 500


def test_repo_newest_decision_ids_is_correct_and_sparse(
    make_city, make_website, db_session
):
    city = make_city()
    website = make_website(city)
    batch = _batch(db_session, city)
    with_two = _job(db_session, batch, status=READY_FOR_APPROVAL, website=website)
    with_one = _job(db_session, batch, status=NEEDS_REVIEW, website=website)
    without = _job(db_session, batch, status=UNSUPPORTED)

    d1 = _decision(db_session, website=website, job=with_two)
    d2 = _decision(db_session, website=website, job=with_two)
    d3 = _decision(db_session, website=website, job=with_one)

    mapping = newest_decision_ids_for_jobs(db_session, [with_two.id, with_one.id, without.id])
    assert mapping[with_two.id] == max(d1.id, d2.id)
    assert mapping[with_one.id] == d3.id
    # A job with no decision is absent, so template `.get` returns None safely.
    assert without.id not in mapping
    assert newest_decision_ids_for_jobs(db_session, []) == {}
