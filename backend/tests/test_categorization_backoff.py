"""How the scheduler backs off AI categorization after a failure. The free
tier allows 20 requests a day, so retrying every hour after the daily quota is
gone, or every minute after a 503, spends requests for nothing."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx

from app.scheduler.runtime import SchedulerRuntime
from app.services import ai_categorization
from app.services.ai_categorizer import (
    GeminiError,
    GeminiQuotaExceeded,
    _retry_after_seconds,
)


def _response(body: dict) -> httpx.Response:
    return httpx.Response(429, json=body)


def test_retry_after_from_retry_info_detail():
    body = {
        "error": {
            "message": "quota",
            "details": [
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "26487s"}
            ],
        }
    }
    assert _retry_after_seconds(_response(body)) == 26487


def test_retry_after_from_message():
    message = "Quota exceeded ... limit: 20 Please retry in 7h21m27.5s."
    assert _retry_after_seconds(_response({"error": {"message": message}})) == 26487.5


def test_retry_after_missing():
    assert _retry_after_seconds(_response({"error": {"message": "nope"}})) is None
    assert _retry_after_seconds(httpx.Response(429, text="not json")) is None


def _runtime(monkeypatch, exc: Exception) -> SchedulerRuntime:
    runtime = SchedulerRuntime(holder="test")
    runtime._is_leader = True

    def boom(*args, **kwargs):
        raise exc

    monkeypatch.setattr(ai_categorization, "drain_categorization_queue", boom)
    return runtime


def _pause(runtime: SchedulerRuntime, started: datetime) -> timedelta:
    return runtime._categorization_cooldown_until - started


def test_daily_quota_pauses_until_google_says(monkeypatch):
    runtime = _runtime(monkeypatch, GeminiQuotaExceeded("429", retry_after_seconds=7 * 3600))
    started = datetime.now(UTC)
    asyncio.run(runtime._categorization_tick())
    assert timedelta(hours=7) <= _pause(runtime, started) <= timedelta(hours=7, minutes=2)


def test_quota_without_a_delay_pauses_an_hour(monkeypatch):
    runtime = _runtime(monkeypatch, GeminiQuotaExceeded("429"))
    started = datetime.now(UTC)
    asyncio.run(runtime._categorization_tick())
    assert timedelta(minutes=59) <= _pause(runtime, started) <= timedelta(hours=1, minutes=1)


def test_other_failures_pause_instead_of_retrying_next_tick(monkeypatch):
    runtime = _runtime(monkeypatch, GeminiError("HTTP 503: overloaded"))
    started = datetime.now(UTC)
    asyncio.run(runtime._categorization_tick())
    assert timedelta(minutes=14) <= _pause(runtime, started) <= timedelta(minutes=16)


def test_repeated_failures_wait_longer_each_time_up_to_a_cap(monkeypatch):
    runtime = _runtime(monkeypatch, GeminiError("HTTP 503: overloaded"))
    pauses = []
    for _ in range(6):
        runtime._categorization_cooldown_until = None
        started = datetime.now(UTC)
        asyncio.run(runtime._categorization_tick())
        pauses.append(round(_pause(runtime, started).total_seconds() / 60))
    assert pauses == [15, 30, 60, 120, 240, 240]


def test_a_success_resets_the_failure_backoff(monkeypatch):
    runtime = _runtime(monkeypatch, GeminiError("HTTP 503: overloaded"))
    runtime._categorization_failures = 3
    monkeypatch.setattr(ai_categorization, "drain_categorization_queue", lambda *a, **k: 0)
    asyncio.run(runtime._categorization_tick())
    assert runtime._categorization_failures == 0
