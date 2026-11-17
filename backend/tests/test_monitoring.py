"""Sentry error monitoring (app.core.monitoring).

Monitoring is off unless SENTRY_DSN is set. These tests turn it on against a
transport that keeps events in memory, so nothing leaves the test run.
"""

from __future__ import annotations

import pytest
import sentry_sdk
from fastapi import FastAPI
from sentry_sdk.transport import Transport
from starlette.testclient import TestClient

from app.config import Settings
from app.core import monitoring
from app.core.exceptions import unhandled_exception_handler
from app.core.logging import get_logger
from app.scheduler.runtime import SchedulerRuntime
from app.services.ai_categorizer import GeminiError


class CapturingTransport(Transport):
    def __init__(self, options=None):
        super().__init__(options)
        self.envelopes = []

    def capture_envelope(self, envelope):
        self.envelopes.append(envelope)

    def events(self) -> list[dict]:
        return [
            item.payload.json
            for envelope in self.envelopes
            for item in envelope.items
            if item.type == "event"
        ]

    def checkins(self) -> list[dict]:
        return [
            item.payload.json
            for envelope in self.envelopes
            for item in envelope.items
            if item.type == "check_in"
        ]


def _settings(**overrides) -> Settings:
    values = {"sentry_dsn": "https://public@sentry.invalid/1", "app_env": "test"}
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def sentry(monkeypatch):
    """Monitoring switched on, with every event kept in ``transport``."""
    transport = CapturingTransport()
    real_init = sentry_sdk.init

    def init_with_capture(*args, **kwargs):
        kwargs["transport"] = transport
        return real_init(*args, **kwargs)

    monkeypatch.setattr(sentry_sdk, "init", init_with_capture)
    monitoring._reset_for_tests()
    yield transport
    sentry_sdk.get_client().close()
    real_init()  # a client with no DSN: sends nothing
    sentry_sdk.get_global_scope().remove_tag("component")
    monitoring._reset_for_tests()


def test_off_without_a_dsn():
    monitoring._reset_for_tests()
    assert monitoring.init_monitoring(_settings(sentry_dsn=None), component="web") is False
    assert monitoring.init_monitoring(_settings(sentry_dsn="  "), component="web") is False
    assert monitoring.is_enabled() is False
    # Neither of these may raise or do anything while monitoring is off.
    monitoring.report_exception(RuntimeError("boom"), job="test")
    monitoring.scheduler_checkin()


def test_background_job_failure_is_reported_with_its_job(sentry):
    assert monitoring.init_monitoring(_settings(), component="scheduler") is True
    try:
        raise GeminiError("response JSON was not a list: '{}'")
    except GeminiError as exc:
        monitoring.report_exception(exc, job="categorization")

    events = sentry.events()
    assert len(events) == 1
    assert events[0]["tags"]["job"] == "categorization"
    assert events[0]["tags"]["component"] == "scheduler"
    assert events[0]["environment"] == "test"
    assert events[0]["exception"]["values"][-1]["type"] == "GeminiError"


def test_the_same_error_repeating_is_sent_once_per_window(sentry, monkeypatch):
    monitoring.init_monitoring(_settings(), component="scheduler")

    def fail(message):
        try:
            raise RuntimeError(message)
        except RuntimeError as exc:
            monitoring.report_exception(exc, job="geocoding")

    for attempt in range(5):
        fail(f"attempt {attempt}")
    assert len(sentry.events()) == 1

    # A different error is not held back.
    try:
        raise ValueError("something else")
    except ValueError as exc:
        monitoring.report_exception(exc, job="geocoding")
    assert len(sentry.events()) == 2

    # Once the window has passed, the repeat is sent again.
    real_monotonic = monitoring.time.monotonic
    monkeypatch.setattr(
        monitoring.time,
        "monotonic",
        lambda: real_monotonic() + monitoring.REPEAT_WINDOW_SECONDS + 1,
    )
    fail("later")
    assert len(sentry.events()) == 3


def test_errors_logged_by_app_loggers_are_reported(sentry):
    # The app's loggers do not propagate to the root logger; Sentry must
    # still see what they log at ERROR.
    monitoring.init_monitoring(_settings(), component="web")
    log = get_logger("test_monitoring")
    log.warning("only a breadcrumb")
    log.error("database pool exhausted")

    events = sentry.events()
    assert len(events) == 1
    assert events[0]["logentry"]["message"] == "database pool exhausted"
    assert events[0]["level"] == "error"


def test_a_crash_in_a_web_request_is_reported_once(sentry):
    monitoring.init_monitoring(_settings(), component="web")
    app = FastAPI()
    app.add_exception_handler(Exception, unhandled_exception_handler)

    @app.get("/boom")
    def boom():
        raise RuntimeError("QueuePool limit of size 5 overflow 10 reached")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/boom", headers={"accept": "application/json"})
    assert response.status_code == 500

    events = sentry.events()
    assert len(events) == 1
    assert events[0]["tags"]["component"] == "web"
    assert events[0]["exception"]["values"][-1]["value"].startswith("QueuePool limit")
    # No personal data: no IP address or cookies are attached.
    assert "ip_address" not in (events[0].get("user") or {})
    assert "cookies" not in events[0].get("request", {})


def test_health_checks_and_static_files_are_never_traced():
    sampler = monitoring._make_traces_sampler(0.5)
    assert sampler({"asgi_scope": {"path": "/health/live"}}) == 0.0
    assert sampler({"asgi_scope": {"path": "/static/app.css"}}) == 0.0
    assert sampler({"asgi_scope": {"path": "/events"}}) == 0.5
    assert monitoring._make_traces_sampler(0.0)({"asgi_scope": {"path": "/events"}}) == 0.0


def test_scheduler_checkins_are_sent_at_most_once_a_minute(sentry):
    monitoring.init_monitoring(_settings(), component="scheduler")
    monitoring.scheduler_checkin()
    monitoring.scheduler_checkin()
    checkins = sentry.checkins()
    assert len(checkins) == 1
    assert checkins[0]["monitor_slug"] == monitoring.SCHEDULER_MONITOR_SLUG
    assert checkins[0]["status"] == "ok"
    assert checkins[0]["monitor_config"]["schedule"]["unit"] == "minute"


@pytest.mark.asyncio
async def test_a_failing_categorization_tick_is_reported(sentry, monkeypatch):
    monitoring.init_monitoring(_settings(), component="scheduler")

    def drain(*args, **kwargs):
        raise GeminiError("response JSON was not a list: '{\"labels\": []}'")

    monkeypatch.setattr("app.services.ai_categorization.drain_categorization_queue", drain)
    runtime = SchedulerRuntime(holder="test")
    runtime._is_leader = True
    await runtime._categorization_tick()

    events = sentry.events()
    assert len(events) == 1
    assert events[0]["tags"]["job"] == "categorization"
    assert "not a list" in events[0]["exception"]["values"][-1]["value"]
    # The tick still backs off as before.
    assert runtime._categorization_cooldown_until is not None


@pytest.mark.asyncio
async def test_the_scheduler_leader_checks_in_on_heartbeat(sentry, db_session):
    monitoring.init_monitoring(_settings(), component="scheduler")
    runtime = SchedulerRuntime(holder="only-process")
    runtime._run_on_start_jobs_now = lambda: None
    await runtime._heartbeat_tick()
    assert runtime._is_leader is True
    assert len(sentry.checkins()) == 1

