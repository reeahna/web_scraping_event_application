"""AI label bookkeeping and monitoring: which events reach the AI, what happens
when the model gives no usable answer, re-queueing when source details change,
admin retries, and the per-request record of tokens and rate limits. No network:
the Groq client runs against an httpx MockTransport or classify_batch is stubbed."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from app.config import get_settings
from app.database import SessionLocal
from app.extraction.types import EventCandidate
from app.models.ai_label_batch import AiLabelBatch
from app.models.event import Event
from app.models.event_category import EventCategory
from app.repositories.event import update_event
from app.services import ai_categorization
from app.services.ai_categorizer import (
    PROMPT_VERSION,
    EventLabel,
    GeminiBadOutput,
    GeminiCategorizer,
    GeminiQuotaExceeded,
    GroqCategorizer,
    parse_limit_detail,
    sanitize_error,
)
from app.services.ai_label_state import (
    LABELED,
    NEEDS_REVIEW,
    STALE,
    ai_input_hash,
    requeue_if_inputs_changed,
)

SOON = date.today() + timedelta(days=3)


@pytest.fixture
def make_event(make_event):
    def _make(city, title="Test Event", **values):
        values.setdefault("start_date", SOON)
        values.setdefault("canonical_url", f"https://x/{title}")
        return make_event(city, title=title, **values)

    return _make


@pytest.fixture
def enabled(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(settings, "groq_api_key", None)
    monkeypatch.setattr(settings, "gemini_scheduled_enabled", True)
    monkeypatch.setattr(settings, "gemini_batch_size", 10)


def _stub(monkeypatch, answer):
    """classify_batch returns answer(events) -> {id: EventLabel}."""
    seen: list[list[int]] = []

    def fake(self, options, events):
        seen.append([e.id for e in events])
        return answer(events)

    monkeypatch.setattr(GeminiCategorizer, "classify_batch", fake)
    return seen


def _music(db_session) -> EventCategory:
    return db_session.query(EventCategory).filter(EventCategory.slug == "music").one()


# --- What reaches the AI -------------------------------------------------------


def test_overridden_and_cancelled_events_are_never_sent(
    db_session, enabled, make_city, make_event, monkeypatch
):
    city = make_city()
    music = _music(db_session)
    overridden = make_event(city, title="Overridden", category_override_id=music.id)
    cancelled = make_event(city, title="Cancelled", is_cancelled=True)
    normal = make_event(city, title="Normal")
    seen = _stub(monkeypatch, lambda events: {e.id: EventLabel("music") for e in events})

    assert ai_categorization.drain_categorization_queue(SessionLocal) == 1
    assert seen == [[normal.id]]
    waiting, labeled = ai_categorization.queue_counts(db_session)
    assert (waiting, labeled) == (0, 1)
    db_session.expire_all()
    assert db_session.get(Event, overridden.id).category_source != "ai"
    assert db_session.get(Event, cancelled.id).category_source != "ai"


def test_a_label_records_bookkeeping(db_session, enabled, make_city, make_event, monkeypatch):
    event = make_event(make_city(), title="Jazz Night")
    _stub(monkeypatch, lambda events: {e.id: EventLabel("music") for e in events})

    ai_categorization.drain_categorization_queue(SessionLocal)

    db_session.expire_all()
    row = db_session.get(Event, event.id)
    assert row.ai_label_status == LABELED
    assert row.ai_attempts == 0
    assert row.ai_input_hash == ai_input_hash(row)
    assert row.ai_prompt_version == PROMPT_VERSION


# --- Unsuccessful answers ------------------------------------------------------


def test_an_omitted_event_moves_to_needs_review_after_two_tries(
    db_session, enabled, make_city, make_event, monkeypatch
):
    city = make_city()
    hard = make_event(city, title="Hard", start_date=SOON)
    easy = make_event(city, title="Easy", start_date=SOON + timedelta(days=1))
    # The model never answers for `hard`.
    seen = _stub(
        monkeypatch,
        lambda events: {e.id: EventLabel("music") for e in events if e.id != hard.id},
    )

    ai_categorization.drain_categorization_queue(SessionLocal)
    db_session.expire_all()
    row = db_session.get(Event, hard.id)
    assert row.ai_attempts == 1
    assert row.ai_label_status is None
    assert "left this event out" in row.ai_last_error

    # A new event jumps ahead of the one that already failed once.
    newer = make_event(city, title="Newer", start_date=SOON + timedelta(days=5))
    ai_categorization.drain_categorization_queue(SessionLocal, limit=1)
    assert seen[-1] == [newer.id]

    ai_categorization.drain_categorization_queue(SessionLocal)
    db_session.expire_all()
    row = db_session.get(Event, hard.id)
    assert row.ai_attempts == 2
    assert row.ai_label_status == NEEDS_REVIEW
    assert db_session.get(Event, easy.id).ai_label_status == LABELED

    # No longer sent, and counted separately.
    calls_before = len(seen)
    assert ai_categorization.drain_categorization_queue(SessionLocal) == 0
    assert len(seen) == calls_before
    status = ai_categorization.queue_status(db_session)
    assert status["waiting"] == 0
    assert status["needs_review"] == 1
    # Still visible: needs-review never hides an event.
    assert row.is_active is True


def test_needs_review_gets_a_weekly_retry_when_nothing_else_waits(
    db_session, enabled, make_city, make_event, monkeypatch
):
    city = make_city()
    old_try = datetime.now(UTC) - timedelta(days=8)
    event = make_event(
        city, title="Stuck", ai_label_status=NEEDS_REVIEW, ai_attempts=2,
        ai_last_attempt_at=old_try,
    )
    recent = make_event(
        city, title="Recent", ai_label_status=NEEDS_REVIEW, ai_attempts=2,
        ai_last_attempt_at=datetime.now(UTC),
    )
    seen = _stub(monkeypatch, lambda events: {e.id: EventLabel("music") for e in events})

    assert ai_categorization.drain_categorization_queue(SessionLocal) == 1
    assert seen == [[event.id]]
    db_session.expire_all()
    assert db_session.get(Event, event.id).ai_label_status == LABELED
    assert db_session.get(Event, recent.id).ai_label_status == NEEDS_REVIEW


def test_unusable_output_counts_an_attempt_but_a_rate_limit_does_not(
    db_session, enabled, make_city, make_event, monkeypatch
):
    event = make_event(make_city(), title="Jazz")

    def bad(self, options, events):
        raise GeminiBadOutput("response was not JSON")

    monkeypatch.setattr(GeminiCategorizer, "classify_batch", bad)
    with pytest.raises(GeminiBadOutput):
        ai_categorization.drain_categorization_queue(SessionLocal)
    db_session.expire_all()
    assert db_session.get(Event, event.id).ai_attempts == 1
    batch = db_session.query(AiLabelBatch).one()
    assert (batch.outcome, batch.events_failed) == ("error", 1)

    def limited(self, options, events):
        raise GeminiQuotaExceeded("HTTP 429", 60.0, "TPD")

    monkeypatch.setattr(GeminiCategorizer, "classify_batch", limited)
    with pytest.raises(GeminiQuotaExceeded):
        ai_categorization.drain_categorization_queue(SessionLocal)
    db_session.expire_all()
    assert db_session.get(Event, event.id).ai_attempts == 1
    assert db_session.query(AiLabelBatch).filter_by(outcome="rate_limited").count() == 1


# --- Re-queue when the source changes -----------------------------------------


def test_changed_words_send_a_labeled_event_back_once(make_city, make_event, db_session):
    event = make_event(make_city(), title="Talk", category_source="ai",
                       ai_prompt_version=PROMPT_VERSION)
    # A label from before hashes existed gets a baseline, not a re-check.
    assert requeue_if_inputs_changed(event) is False
    assert event.ai_input_hash == ai_input_hash(event)
    assert requeue_if_inputs_changed(event) is False

    event.description = "Now a concert with a live band"
    assert requeue_if_inputs_changed(event) is True
    assert event.ai_label_status == STALE
    db_session.commit()
    assert event.id in {e.id for e in ai_categorization.pending_events(db_session)}


def test_changed_words_give_a_needs_review_event_fresh_tries(make_city, make_event):
    event = make_event(make_city(), title="Odd", ai_label_status=NEEDS_REVIEW, ai_attempts=2)
    event.ai_input_hash = ai_input_hash(event)
    event.title = "Odd, now with a description"
    assert requeue_if_inputs_changed(event) is True
    assert (event.ai_label_status, event.ai_attempts) == (None, 0)


def _candidate(**values) -> EventCandidate:
    base = dict(
        raw={}, title="Talk", canonical_url="https://x/Talk", description=None,
        start_date=SOON, start_time=None, end_date=None, end_time=None,
        timezone=None, venue=None, address=None, image_url=None,
        latitude=None, longitude=None, source_category=None, external_source_id=None,
        field_source_paths={}, transformation_history=(),
        source_page="https://x", extraction_pattern="json_ld_event",
        warnings=(), raw_record_hash="h",
    )
    base.update(values)
    return EventCandidate(**base)


def test_rescrape_requeues_only_when_the_model_inputs_change(
    db_session, make_city, make_event
):
    event = make_event(make_city(), title="Talk", category_source="ai",
                       ai_prompt_version=PROMPT_VERSION, ai_label_status=LABELED)
    event.ai_input_hash = ai_input_hash(event)
    db_session.commit()

    # A new image or time is not something the model reads.
    update_event(db_session, event, _candidate(image_url="https://x/i.png"))
    assert event.ai_label_status == LABELED

    update_event(db_session, event, _candidate(description="An evening of jazz"))
    assert event.ai_label_status == STALE
    assert event.category_source == "ai"  # keeps its label until the new one


# --- Administrator decisions --------------------------------------------------


def test_ai_never_hides_an_event_an_admin_set_visible(
    db_session, enabled, make_city, make_event, monkeypatch
):
    event = make_event(make_city(), title="Seminar",
                       visibility_set_by_admin_at=datetime.now(UTC))
    _stub(monkeypatch, lambda events: {e.id: EventLabel("music", keep=False) for e in events})

    ai_categorization.drain_categorization_queue(SessionLocal)
    db_session.expire_all()
    row = db_session.get(Event, event.id)
    assert row.category_source == "ai"
    assert row.is_active is True


@pytest.fixture
def admin_client(client, make_super_admin, login):
    make_super_admin(email="ai-root@example.com")
    login("ai-root@example.com")
    return client


def test_admin_page_and_retry(admin_client, db_session, enabled, make_city, make_event):
    city = make_city()
    stuck = make_event(city, title="Stuck Event", ai_label_status=NEEDS_REVIEW, ai_attempts=2,
                       ai_last_error="the model left this event out of its answer")
    other = make_event(city, title="Other Stuck", ai_label_status=NEEDS_REVIEW, ai_attempts=2)
    db_session.add(AiLabelBatch(provider="groq", model="openai/gpt-oss-120b",
                                outcome="rate_limited", http_status=429, events_submitted=25,
                                events_failed=25, limit_kind="TPD", limit_value=200000,
                                limit_used=199000, retry_after_seconds=1660.0))
    db_session.commit()

    page = admin_client.get("/admin/ai-labeling")
    assert page.status_code == 200
    assert "Stuck Event" in page.text
    assert "TPD" in page.text
    assert admin_client.get("/admin/ai-labeling/status").json()["needs_review_total"] == 2

    csrf = admin_client.cookies.get("csrf_token")
    resp = admin_client.post(
        f"/admin/ai-labeling/events/{stuck.id}/retry",
        data={"csrf_token": csrf, "next": f"/admin/events/{stuck.id}"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/admin/events/{stuck.id}"
    db_session.expire_all()
    assert db_session.get(Event, stuck.id).ai_label_status is None
    assert db_session.get(Event, stuck.id).ai_attempts == 0

    resp = admin_client.post("/admin/ai-labeling/retry-all", data={"csrf_token": csrf},
                             follow_redirects=False)
    assert resp.status_code == 303
    db_session.expire_all()
    assert db_session.get(Event, other.id).ai_label_status is None


def test_retry_never_redirects_off_the_admin(admin_client, make_city, make_event):
    event = make_event(make_city(), title="X", ai_label_status=NEEDS_REVIEW)
    resp = admin_client.post(
        f"/admin/ai-labeling/events/{event.id}/retry",
        data={"csrf_token": admin_client.cookies.get("csrf_token"),
              "next": "https://evil.example/"},
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/admin"


def test_admin_activation_is_remembered(admin_client, db_session, make_city, make_event):
    event = make_event(make_city(), title="Hidden", is_active=False)
    admin_client.post(
        f"/admin/events/{event.id}/lifecycle",
        data={"csrf_token": admin_client.cookies.get("csrf_token"), "action": "activate"},
    )
    db_session.expire_all()
    assert db_session.get(Event, event.id).visibility_set_by_admin_at is not None


# --- Monitoring of each request ------------------------------------------------


def _groq_with(handler) -> GroqCategorizer:
    categorizer = GroqCategorizer("gsk_secret", "openai/gpt-oss-120b", max_retries=0,
                                  min_interval_seconds=0)
    categorizer._client = httpx.Client(transport=httpx.MockTransport(handler))
    return categorizer


RATE_HEADERS = {
    "x-ratelimit-limit-requests": "1000",
    "x-ratelimit-remaining-requests": "950",
    "x-ratelimit-reset-requests": "1h12m",
    "x-ratelimit-limit-tokens": "8000",
    "x-ratelimit-remaining-tokens": "4000",
    "x-ratelimit-reset-tokens": "29.5s",
}


def test_success_records_usage_and_rate_headers(db_session, make_city, make_event):
    from app.services.ai_categorizer import CategoryOption, EventToClassify

    def handler(request):
        content = json.dumps({"labels": [{"id": 1, "category": "music", "keep": True}]})
        body = {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 3000, "completion_tokens": 400, "total_tokens": 3400,
                      "prompt_tokens_details": {"cached_tokens": 800},
                      "completion_tokens_details": {"reasoning_tokens": 150}},
        }
        return httpx.Response(200, json=body, headers=RATE_HEADERS)

    categorizer = _groq_with(handler)
    labels = categorizer.classify_batch(
        [CategoryOption("music", "Music")], [EventToClassify(1, "Jazz")]
    )
    assert labels == {1: EventLabel("music", True)}
    stats = categorizer.last_stats
    assert (stats.prompt_tokens, stats.completion_tokens, stats.total_tokens) == (3000, 400, 3400)
    assert (stats.cached_tokens, stats.reasoning_tokens) == (800, 150)
    assert (stats.remaining_requests, stats.remaining_tokens) == (950, 4000)
    assert stats.reset_tokens == "29.5s"
    assert stats.http_status == 200


def test_a_429_names_the_limit_without_leaking_ids(db_session):
    from app.services.ai_categorizer import CategoryOption, EventToClassify

    message = (
        "Rate limit reached for model `openai/gpt-oss-120b` in organization "
        "`org_01abcDEF` service tier `on_demand` on tokens per day (TPD): Limit 200000, "
        "Used 199102, Requested 4015. Please try again in 27m41.5s."
    )

    def handler(request):
        return httpx.Response(
            429, json={"error": {"message": message}},
            headers={**RATE_HEADERS, "retry-after": "1662"},
        )

    categorizer = _groq_with(handler)
    with pytest.raises(GeminiQuotaExceeded) as caught:
        categorizer.classify_batch([CategoryOption("music", "Music")], [EventToClassify(1, "J")])
    assert caught.value.limit_kind == "TPD"
    stats = categorizer.last_stats
    assert (stats.limit_kind, stats.limit_value, stats.limit_used, stats.limit_requested) == (
        "TPD", 200000, 199102, 4015,
    )
    assert stats.retry_after_seconds == 1662.0
    assert "org_01abcDEF" not in stats.error
    assert "org_01abcDEF" not in str(caught.value)


def test_a_400_is_bad_output():
    from app.services.ai_categorizer import CategoryOption, EventToClassify

    def handler(request):
        return httpx.Response(400, json={"error": {"message": "json_validate_failed"}})

    with pytest.raises(GeminiBadOutput):
        _groq_with(handler).classify_batch(
            [CategoryOption("music", "Music")], [EventToClassify(1, "J")]
        )


def test_drain_writes_one_batch_row_per_request(
    db_session, make_city, make_event, monkeypatch
):
    settings = get_settings()
    monkeypatch.setattr(settings, "groq_api_key", "gsk_test")
    monkeypatch.setattr(settings, "gemini_scheduled_enabled", True)
    event = make_event(make_city(), title="Jazz")

    def handler(request):
        assert request.headers["authorization"] == "Bearer gsk_test"
        content = json.dumps({"labels": [{"id": event.id, "category": "music", "keep": True}]})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}],
                  "usage": {"prompt_tokens": 900, "completion_tokens": 60,
                            "total_tokens": 960}},
            headers=RATE_HEADERS,
        )

    real_init = GroqCategorizer.__init__

    def init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        self._client = httpx.Client(
            transport=httpx.MockTransport(handler), headers=self._client.headers
        )

    monkeypatch.setattr(GroqCategorizer, "__init__", init)

    assert ai_categorization.drain_categorization_queue(SessionLocal) == 1
    batch = db_session.query(AiLabelBatch).one()
    assert (batch.provider, batch.model, batch.outcome) == ("groq", settings.groq_model, "ok")
    assert (batch.events_submitted, batch.events_labeled, batch.events_skipped) == (1, 1, 0)
    assert (batch.prompt_tokens, batch.total_tokens, batch.remaining_requests) == (900, 960, 950)
    summary = ai_categorization.usage_summary(db_session)
    assert summary["tokens_per_labeled_event"] == 960
    ready = ai_categorization.queue_status(db_session)
    assert ready["last_24h"]["total_tokens"] == 960


def test_old_batch_rows_are_pruned(db_session):
    db_session.add(AiLabelBatch(provider="groq", model="m", outcome="ok",
                                created_at=datetime.now(UTC) - timedelta(days=31)))
    db_session.add(AiLabelBatch(provider="groq", model="m", outcome="ok"))
    db_session.commit()
    assert ai_categorization.prune_batch_history(db_session) == 1


def test_error_text_is_sanitized():
    text = sanitize_error("bad key gsk_abc123 for org_XYZ and AIzaSyA1234567890123456789012")
    assert "gsk_abc123" not in text and "org_XYZ" not in text and "AIza" not in text
    assert parse_limit_detail("nothing here")["kind"] is None
