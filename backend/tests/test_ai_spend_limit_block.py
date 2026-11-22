"""Groq's spend limit (HTTP 400, error code "blocked_api_access") is a billing
block, not an answer about the events: it is handled like a quota (the model
waits and is asked again later) and never counts as an unusable answer, so no
event gains a failed attempt or moves to needs-review because of it. The Groq
client runs against an httpx MockTransport; no network."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from app.config import get_settings
from app.database import SessionLocal
from app.models.ai_label_batch import AiLabelBatch
from app.models.event import Event
from app.scheduler.runtime import SchedulerRuntime
from app.services import ai_categorization
from app.services.ai_categorizer import (
    ACCESS_BLOCKED_RETRY_SECONDS,
    SPEND_LIMIT_KIND,
    CategoryOption,
    EventToClassify,
    GeminiBadOutput,
    GeminiQuotaExceeded,
    GroqCategorizer,
    ProviderAccessBlocked,
)
from app.services.ai_label_state import NEEDS_REVIEW

SOON = date.today() + timedelta(days=3)
MAIN = "openai/gpt-oss-120b"
BACKUP = "openai/gpt-oss-20b"

# The body Groq documents for a spend-limit block.
BLOCKED_BODY = {
    "error": {
        "message": "API access is blocked: your organization has reached its spend limit.",
        "type": "invalid_request_error",
        "code": "blocked_api_access",
    }
}


def _blocked(request: httpx.Request) -> httpx.Response:
    return httpx.Response(400, json=BLOCKED_BODY)


def _groq_with(handler) -> GroqCategorizer:
    categorizer = GroqCategorizer("gsk_secret", MAIN, max_retries=0, min_interval_seconds=0)
    categorizer._client = httpx.Client(transport=httpx.MockTransport(handler))
    return categorizer


@pytest.fixture
def groq(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "groq_api_key", "gsk_test")
    monkeypatch.setattr(settings, "groq_model", MAIN)
    monkeypatch.setattr(settings, "groq_fallback_model", None)
    monkeypatch.setattr(settings, "gemini_scheduled_enabled", True)
    monkeypatch.setattr(settings, "gemini_hide_unwanted", True)
    return settings


@pytest.fixture
def transport(monkeypatch):
    """Route every GroqCategorizer the drain builds through `state["handler"]`
    and count the requests that reach it."""
    state = {"handler": _blocked, "calls": []}

    def handler(request):
        state["calls"].append(json.loads(request.content)["model"])
        return state["handler"](request)

    real_init = GroqCategorizer.__init__

    def init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        self._client = httpx.Client(
            transport=httpx.MockTransport(handler), headers=self._client.headers
        )

    monkeypatch.setattr(GroqCategorizer, "__init__", init)
    return state


@pytest.fixture
def event(make_city, make_event):
    city = make_city()
    counter = iter(range(1000))

    def _event(title="Jazz Night", **values):
        values.setdefault("start_date", SOON)
        return make_event(
            city, title=title, canonical_url=f"https://x/{next(counter)}", **values
        )

    return _event


def _drain_blocked(now: datetime | None = None) -> None:
    with pytest.raises(GeminiQuotaExceeded):
        ai_categorization.drain_categorization_queue(SessionLocal, now=now)


# --- The client --------------------------------------------------------------


def test_a_spend_limit_block_is_a_quota_wait_not_bad_output():
    categorizer = _groq_with(_blocked)
    with pytest.raises(ProviderAccessBlocked) as caught:
        categorizer.classify_batch(
            [CategoryOption("music", "Music")], [EventToClassify(1, "Jazz")]
        )
    assert isinstance(caught.value, GeminiQuotaExceeded)
    assert not isinstance(caught.value, GeminiBadOutput)
    assert caught.value.retry_after_seconds == ACCESS_BLOCKED_RETRY_SECONDS
    assert caught.value.limit_kind == SPEND_LIMIT_KIND
    stats = categorizer.last_stats
    assert stats.http_status == 400
    assert (stats.limit_kind, stats.retry_after_seconds) == (
        SPEND_LIMIT_KIND, ACCESS_BLOCKED_RETRY_SECONDS,
    )
    assert "blocked" in stats.error


def test_the_block_is_recognised_by_its_message_too():
    def handler(request):
        return httpx.Response(
            400, json={"error": {"message": "blocked_api_access: spend limit reached"}}
        )

    with pytest.raises(ProviderAccessBlocked):
        _groq_with(handler).classify_batch(
            [CategoryOption("music", "Music")], [EventToClassify(1, "J")]
        )


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"message": "json_validate_failed", "code": "json_validate_failed"}},
        {"error": {"message": "Failed to generate JSON"}},
        {"error": "a plain string"},
    ],
)
def test_other_400s_are_still_bad_output(body):
    def handler(request):
        return httpx.Response(400, json=body)

    with pytest.raises(GeminiBadOutput) as caught:
        _groq_with(handler).classify_batch(
            [CategoryOption("music", "Music")], [EventToClassify(1, "J")]
        )
    assert not isinstance(caught.value, ProviderAccessBlocked)


def test_a_400_without_json_is_still_bad_output():
    def handler(request):
        return httpx.Response(400, text="bad request")

    with pytest.raises(GeminiBadOutput):
        _groq_with(handler).classify_batch(
            [CategoryOption("music", "Music")], [EventToClassify(1, "J")]
        )


# --- The drain ---------------------------------------------------------------


def test_a_block_changes_nothing_on_the_events(db_session, groq, transport, event):
    events = [event(title=f"Show {i}") for i in range(3)]
    before = {
        e.id: (e.category_id, e.category_source, e.is_active, e.ai_label_status)
        for e in events
    }

    _drain_blocked()

    db_session.expire_all()
    for e in events:
        row = db_session.get(Event, e.id)
        assert row.ai_attempts == 0
        assert row.ai_last_error is None
        assert (row.category_id, row.category_source, row.is_active, row.ai_label_status) == (
            before[e.id]
        )
    batch = db_session.query(AiLabelBatch).one()
    assert (batch.outcome, batch.http_status, batch.limit_kind) == ("blocked", 400, "SPEND")
    assert batch.retry_after_seconds == ACCESS_BLOCKED_RETRY_SECONDS
    assert (batch.events_submitted, batch.events_labeled, batch.events_skipped) == (3, 0, 0)


def test_repeated_blocks_never_move_events_to_needs_review(db_session, groq, transport, event):
    # One event already has an unusable answer on record: a block must not
    # be the second strike that sends it to needs-review.
    struck = event(title="Struck once", ai_attempts=1, ai_last_error="unusable answer")
    fresh = [event(title=f"Fresh {i}") for i in range(2)]
    start = datetime.now(UTC)

    for hour in range(6):
        _drain_blocked(now=start + timedelta(hours=2 * hour))

    assert len(transport["calls"]) == 6
    db_session.expire_all()
    assert db_session.get(Event, struck.id).ai_attempts == 1
    for e in fresh:
        assert db_session.get(Event, e.id).ai_attempts == 0
    assert db_session.query(Event).filter(Event.ai_label_status == NEEDS_REVIEW).count() == 0
    assert ai_categorization.needs_review_events(db_session).count() == 0
    waiting, _labeled = ai_categorization.queue_counts(db_session)
    assert waiting == 3


def test_the_model_waits_an_hour_and_remembers_it(db_session, groq, transport, event):
    event()
    start = datetime.now(UTC)
    _drain_blocked(now=start)

    until = ai_categorization.model_cooldown_until(db_session, MAIN, now=start)
    assert until is not None
    assert timedelta(minutes=59) <= until - start <= timedelta(hours=1, minutes=1)

    # Inside the wait the model is not asked again (survives a restart: the
    # wait comes from the stored row).
    _drain_blocked(now=start + timedelta(minutes=30))
    assert len(transport["calls"]) == 1

    # After the wait a lifted block means labeling simply resumes.
    def answer(request):
        sent = json.loads(json.loads(request.content)["messages"][-1]["content"]
                          .split("Events:\n", 1)[1])
        content = json.dumps(
            {"labels": [{"id": item["id"], "category": "music", "keep": True} for item in sent]}
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    transport["handler"] = answer
    later = start + timedelta(hours=1, minutes=5)
    assert ai_categorization.drain_categorization_queue(SessionLocal, now=later) == 1
    assert ai_categorization.model_cooldown_until(db_session, MAIN, now=later) is None


def test_usage_and_health_count_blocked_requests(db_session, groq, transport, event):
    event()
    _drain_blocked()
    usage = ai_categorization.usage_summary(db_session)
    assert (usage["requests"], usage["blocked"], usage["rate_limited"], usage["ok"]) == (
        1, 1, 0, 0,
    )
    status = ai_categorization.queue_status(db_session)
    assert status["last_24h"]["blocked"] == 1
    assert status["needs_review"] == 0


def test_with_a_backup_model_the_block_touches_nothing_either(
    db_session, groq, transport, event
):
    # The spend limit is per organization, so the backup is blocked too. No
    # provisional label, no backup attempt, nothing hidden.
    groq.groq_fallback_model = BACKUP
    e = event()

    _drain_blocked()

    assert transport["calls"] == [MAIN, BACKUP]
    db_session.expire_all()
    row = db_session.get(Event, e.id)
    assert row.ai_attempts == 0
    assert row.ai_fallback_attempts == 0
    assert row.category_source != "ai_provisional"
    assert row.is_active
    outcomes = sorted(
        (b.model, b.outcome, b.provisional) for b in db_session.query(AiLabelBatch).all()
    )
    assert outcomes == [(MAIN, "blocked", False), (BACKUP, "blocked", True)]


# --- The scheduler -----------------------------------------------------------


def test_the_scheduler_pauses_about_an_hour_without_error_backoff(monkeypatch):
    runtime = SchedulerRuntime(holder="test")
    runtime._is_leader = True
    reported = []

    def blocked(*args, **kwargs):
        raise ProviderAccessBlocked("HTTP 400", ACCESS_BLOCKED_RETRY_SECONDS, SPEND_LIMIT_KIND)

    monkeypatch.setattr(ai_categorization, "drain_categorization_queue", blocked)
    monkeypatch.setattr(
        "app.scheduler.runtime.report_exception", lambda *a, **k: reported.append(a)
    )
    started = datetime.now(UTC)
    asyncio.run(runtime._categorization_tick())
    pause = runtime._categorization_cooldown_until - started
    assert timedelta(minutes=60) <= pause <= timedelta(minutes=62)
    assert runtime._categorization_failures == 0
    assert reported == []


def test_the_admin_page_names_the_block(client, make_super_admin, login, db_session):
    make_super_admin(email="spend-root@example.com")
    login("spend-root@example.com")
    db_session.add(AiLabelBatch(
        provider="groq", model=MAIN, outcome="blocked", http_status=400,
        events_submitted=25, events_failed=25, limit_kind="SPEND",
        retry_after_seconds=ACCESS_BLOCKED_RETRY_SECONDS,
    ))
    db_session.commit()
    page = client.get("/admin/ai-labeling")
    assert page.status_code == 200
    assert "Blocked by spend limit" in page.text
    assert "1 blocked by the spend limit" in page.text
