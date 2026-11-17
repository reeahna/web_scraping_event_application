"""The admin System health page and the background job status it reads.

The page exists so a failure like AI categorization silently stopping shows
up at a glance, so most of these tests set up one broken thing and check the
summary names it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

from app.core.permissions import ADMINISTRATOR
from app.database import SessionLocal
from app.models.background_job import BackgroundJobStatus
from app.scheduler.runtime import SchedulerRuntime
from app.services import ai_categorization
from app.services.ai_categorizer import GeminiError
from app.services.job_status import job_statuses, record_job_failure, record_job_success
from app.services.system_health import collect_system_health, time_ago


def _admin(make_user, login, email="health@example.com"):
    make_user(email=email, password="health-pass-1234", role_name=ADMINISTRATOR)
    login(email, "health-pass-1234")


# --- job status recording ---------------------------------------------------------


def test_success_then_failure_then_success_is_recorded(db_session):
    record_job_success(SessionLocal, "alerts", "sent 2")
    record_job_failure(SessionLocal, "alerts", "boom")
    record_job_failure(SessionLocal, "alerts", "boom again")
    db_session.expire_all()
    row = job_statuses(db_session)["alerts"]
    assert row.last_status == "failed"
    assert row.last_error == "boom again"
    assert row.consecutive_failures == 2
    assert row.last_result == "sent 2"

    record_job_success(SessionLocal, "alerts", "sent 1")
    db_session.expire_all()
    row = job_statuses(db_session)["alerts"]
    assert row.last_status == "ok"
    assert row.consecutive_failures == 0
    assert db_session.query(BackgroundJobStatus).count() == 1


def test_long_errors_are_trimmed(db_session):
    record_job_failure(SessionLocal, "geocoding", "x" * 5000)
    db_session.expire_all()
    assert len(job_statuses(db_session)["geocoding"].last_error) == 1000


def test_categorization_failure_is_recorded_by_the_scheduler(monkeypatch, db_session):
    runtime = SchedulerRuntime(holder="test")
    runtime._is_leader = True

    def boom(*args, **kwargs):
        raise GeminiError("HTTP 503: overloaded")

    monkeypatch.setattr(ai_categorization, "drain_categorization_queue", boom)
    asyncio.run(runtime._categorization_tick())

    db_session.expire_all()
    row = job_statuses(db_session)["categorization"]
    assert row.last_status == "failed"
    assert "overloaded" in row.last_error
    assert row.paused_until is not None


def test_categorization_that_labels_nothing_while_events_wait_is_a_failure(
    monkeypatch, db_session, make_city, make_event
):
    """Today's silent failure: the AI answered, nothing got labeled, no error
    was raised. That must now show up as a failure."""
    city = make_city()
    make_event(city, start_date=date.today() + timedelta(days=2))
    runtime = SchedulerRuntime(holder="test")
    runtime._is_leader = True
    monkeypatch.setattr(ai_categorization, "drain_categorization_queue", lambda *a, **k: 0)
    asyncio.run(runtime._categorization_tick())

    db_session.expire_all()
    row = job_statuses(db_session)["categorization"]
    assert row.last_status == "failed"
    assert "labeled none" in row.last_error


def test_categorization_with_nothing_waiting_records_nothing(monkeypatch, db_session):
    runtime = SchedulerRuntime(holder="test")
    runtime._is_leader = True
    monkeypatch.setattr(ai_categorization, "drain_categorization_queue", lambda *a, **k: 0)
    asyncio.run(runtime._categorization_tick())
    assert "categorization" not in job_statuses(db_session)


def test_categorization_success_is_recorded(monkeypatch, db_session):
    runtime = SchedulerRuntime(holder="test")
    runtime._is_leader = True
    monkeypatch.setattr(ai_categorization, "drain_categorization_queue", lambda *a, **k: 7)
    asyncio.run(runtime._categorization_tick())
    db_session.expire_all()
    row = job_statuses(db_session)["categorization"]
    assert row.last_status == "ok"
    assert row.last_result == "labeled 7 event(s)"


# --- summary ----------------------------------------------------------------------


def test_time_ago():
    now = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
    assert time_ago(None, now) == "never"
    assert time_ago(now - timedelta(seconds=20), now) == "just now"
    assert time_ago(now - timedelta(minutes=1), now) == "1 minute ago"
    assert time_ago(now - timedelta(hours=5), now) == "5 hours ago"
    assert time_ago(now - timedelta(days=2, hours=3), now) == "2 days ago"
    assert time_ago(now + timedelta(minutes=30), now) == "in 30 minutes"


def test_towns_without_upcoming_events_are_flagged_and_listed_first(
    db_session, make_city, make_event
):
    busy = make_city(name="Busy Town", slug="busy-town")
    make_city(name="Empty Town", slug="empty-town")
    make_event(busy, start_date=date.today() + timedelta(days=1))
    make_event(busy, title="Past", canonical_url="https://e/2",
               start_date=date.today() - timedelta(days=5))

    health = collect_system_health(db_session)
    assert [t.name for t in health.towns] == ["Empty Town", "Busy Town"]
    assert health.towns[1].upcoming_events == 1
    assert health.towns_without_events == 1
    assert any("1 of 2 towns have no upcoming events" in i.text for i in health.issues)


def test_scheduler_never_run_is_a_problem(db_session):
    health = collect_system_health(db_session)
    problems = [i.text for i in health.issues if i.severity == "problem"]
    assert any("scheduler has never run" in text for text in problems)


def test_failed_job_is_a_problem(db_session):
    record_job_failure(SessionLocal, "alerts", "SMTP refused")
    health = collect_system_health(db_session)
    assert any(
        i.severity == "problem" and "Alerts and reminders failed: SMTP refused" in i.text
        for i in health.issues
    )
    job = next(j for j in health.jobs if j.key == "alerts")
    assert job.state == "failed"


def test_daily_job_without_a_recent_success_is_overdue(db_session, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "college_town_import_enabled", True)
    record_job_success(
        SessionLocal, "college_towns", "ok", now=datetime.now(UTC) - timedelta(days=3)
    )
    health = collect_system_health(db_session)
    job = next(j for j in health.jobs if j.key == "college_towns")
    assert job.state == "overdue"
    assert any("Daily college town import has not run since" in i.text for i in health.issues)


def test_failing_source_is_listed(db_session, make_city, make_website):
    city = make_city()
    website = make_website(city, name="Broken Calendar", is_active=True)
    website.consecutive_failure_count = 4
    db_session.commit()

    health = collect_system_health(db_session)
    assert health.sources_failing == 1
    assert health.failing_sources[0].name == "Broken Calendar"
    assert health.failing_sources[0].failures == 4


# --- the page ---------------------------------------------------------------------


def test_page_renders_every_section(client, make_user, login, make_city):
    make_city(name="Quiet Town", slug="quiet-town")
    _admin(make_user, login)
    resp = client.get("/admin/health")
    assert resp.status_code == 200
    for heading in (
        "System health", "Scheduler", "Scraping", "AI categorization",
        "Recurring jobs", "Towns", "Approvals and broken sources", "Deploy",
    ):
        assert heading in resp.text
    assert "Quiet Town" in resp.text
    assert "health-row-empty" in resp.text


def test_page_shows_ai_error(client, make_user, login, monkeypatch):
    monkeypatch.setattr(ai_categorization, "scheduled_categorization_enabled", lambda: True)
    monkeypatch.setattr(ai_categorization, "provider_name", lambda: "groq")
    record_job_failure(SessionLocal, "categorization", "GroqError: bad JSON shape")
    _admin(make_user, login, "health-ai@example.com")
    resp = client.get("/admin/health")
    assert "AI categorization is failing: GroqError: bad JSON shape" in resp.text


def test_page_needs_sites_view(client, make_user, login):
    make_user(email="plain@example.com", password="plain-pass-1234")
    login("plain@example.com", "plain-pass-1234")
    assert client.get("/admin/health").status_code == 403


def test_dashboard_links_to_health_with_a_count(client, make_user, login):
    _admin(make_user, login, "health-dash@example.com")
    resp = client.get("/admin")
    assert 'href="/admin/health"' in resp.text
    assert "problem" in resp.text  # the scheduler has never run in tests


def test_production_blockers_are_one_summary_line(db_session, monkeypatch):
    from app.services import system_health

    class Blocker:
        def __init__(self, message):
            self.severity, self.message, self.code = "blocker", message, "x"

    monkeypatch.setattr(
        system_health, "production_blockers", lambda s: [Blocker("a"), Blocker("b")]
    )
    health = collect_system_health(db_session)
    lines = [i.text for i in health.issues if i.url == "#deploy"]
    assert lines == ["2 production settings need attention."]
    assert len(health.blockers) == 2
