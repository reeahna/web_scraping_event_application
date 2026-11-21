"""The optional backup labeling model (GROQ_FALLBACK_MODEL): used only while
the main model is rate limited, its categories are provisional, it never
hides an event or replaces a main-model label or an admin override, and every
event it labels stays queued for the main model. classify_batch is stubbed;
no network."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from itertools import count

import pytest

from app.config import Settings, get_settings
from app.database import SessionLocal
from app.models.ai_label_batch import AiLabelBatch
from app.models.event import Event
from app.models.event_category import EventCategory
from app.services import ai_categorization
from app.services.ai_categorizer import (
    PROMPT_VERSION,
    EventLabel,
    GeminiBadOutput,
    GeminiQuotaExceeded,
    GeminiTransportError,
    GroqCategorizer,
)
from app.services.ai_label_state import (
    LABELED,
    NEEDS_REVIEW,
    PROVISIONAL,
    PROVISIONAL_SOURCE,
    STALE,
    requeue_if_inputs_changed,
)

SOON = date.today() + timedelta(days=3)
MAIN = "openai/gpt-oss-120b"
BACKUP = "openai/gpt-oss-20b"
_urls = count()


@pytest.fixture
def groq(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "groq_api_key", "gsk_test")
    monkeypatch.setattr(settings, "groq_model", MAIN)
    monkeypatch.setattr(settings, "groq_fallback_model", BACKUP)
    monkeypatch.setattr(settings, "gemini_scheduled_enabled", True)
    monkeypatch.setattr(settings, "gemini_hide_unwanted", True)


@pytest.fixture
def event(make_city, make_event):
    city = make_city()

    def _event(title="Jazz Night", **values):
        values.setdefault("start_date", SOON)
        return make_event(city, title=title, canonical_url=f"https://x/{next(_urls)}", **values)

    return _event


def _stub(monkeypatch, *, limited=(), keep=True, category="music", fail=None, during=None):
    """Replace the request. `limited` models answer 429; `fail` maps a model
    to an exception to raise; `during(model)` runs while the request is in
    flight (to simulate something else changing the event)."""
    calls: list[str] = []

    def fake(self, options, events):
        calls.append(self.model)
        if during:
            during(self.model)
        if self.model in limited:
            self.last_stats.retry_after_seconds = 1200.0
            raise GeminiQuotaExceeded("HTTP 429", 1200.0, "TPD")
        if fail and self.model in fail:
            raise fail[self.model]
        return {e.id: EventLabel(category, keep) for e in events}

    monkeypatch.setattr(GroqCategorizer, "classify_batch", fake)
    return calls


def _limited(db_session, model, *, minutes_ago=1, wait=1200.0):
    db_session.add(AiLabelBatch(
        provider="groq", model=model, outcome="rate_limited", retry_after_seconds=wait,
        created_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
    ))
    db_session.commit()


def _reload(db_session, event_id):
    db_session.expire_all()
    return db_session.get(Event, event_id)


# --- Off by default and only after the main quota is used up ------------------


def test_the_backup_is_off_by_default():
    assert Settings().groq_fallback_model is None


def test_without_a_backup_the_main_limit_stops_labeling(
    db_session, groq, event, monkeypatch
):
    monkeypatch.setattr(get_settings(), "groq_fallback_model", None)
    event()
    calls = _stub(monkeypatch, limited={MAIN})
    with pytest.raises(GeminiQuotaExceeded):
        ai_categorization.drain_categorization_queue(SessionLocal)
    assert calls == [MAIN]


def test_the_main_model_is_asked_first_when_it_has_quota(db_session, groq, event, monkeypatch):
    item = event()
    calls = _stub(monkeypatch)
    assert ai_categorization.drain_categorization_queue(SessionLocal) == 1
    assert calls == [MAIN]
    row = _reload(db_session, item.id)
    assert (row.category_source, row.ai_label_status, row.ai_model) == ("ai", LABELED, MAIN)


def test_a_main_outage_never_switches_to_the_backup(db_session, groq, event, monkeypatch):
    event()
    calls = _stub(monkeypatch, fail={MAIN: GeminiTransportError("timed out")})
    with pytest.raises(GeminiTransportError):
        ai_categorization.drain_categorization_queue(SessionLocal)
    assert calls == [MAIN]


def test_a_429_from_the_main_model_uses_the_backup_in_the_same_tick(
    db_session, groq, event, monkeypatch
):
    item = event()
    calls = _stub(monkeypatch, limited={MAIN})
    assert ai_categorization.drain_categorization_queue(SessionLocal) == 1
    assert calls == [MAIN, BACKUP]
    row = _reload(db_session, item.id)
    assert row.category.slug == "music"
    assert (row.category_source, row.ai_label_status, row.ai_model) == (
        PROVISIONAL_SOURCE, PROVISIONAL, BACKUP)
    assert row.ai_prompt_version is None
    batch = db_session.query(AiLabelBatch).filter_by(model=BACKUP).one()
    assert batch.provisional is True and batch.events_hidden == 0


def test_a_waiting_main_model_is_not_asked(db_session, groq, event, monkeypatch):
    event()
    _limited(db_session, MAIN)
    calls = _stub(monkeypatch)
    ai_categorization.drain_categorization_queue(SessionLocal)
    assert calls == [BACKUP]


def test_when_both_models_wait_nothing_is_sent(db_session, groq, event, monkeypatch):
    event()
    _limited(db_session, MAIN, wait=3000.0)
    _limited(db_session, BACKUP, wait=600.0)
    calls = _stub(monkeypatch)
    with pytest.raises(GeminiQuotaExceeded) as caught:
        ai_categorization.drain_categorization_queue(SessionLocal)
    assert calls == []
    # The scheduler sleeps until the first model is free again.
    assert 500 < caught.value.retry_after_seconds < 700


def test_a_429_from_the_backup_starts_its_own_wait(db_session, groq, event, monkeypatch):
    item = event()
    _limited(db_session, MAIN)
    calls = _stub(monkeypatch, limited={BACKUP})
    with pytest.raises(GeminiQuotaExceeded):
        ai_categorization.drain_categorization_queue(SessionLocal)
    assert ai_categorization.model_cooldown_until(db_session, BACKUP) is not None
    with pytest.raises(GeminiQuotaExceeded):
        ai_categorization.drain_categorization_queue(SessionLocal)
    assert calls == [BACKUP]
    row = _reload(db_session, item.id)
    assert row.category_source != PROVISIONAL_SOURCE and row.ai_fallback_attempts == 0


# --- Never hides, never overwrites -------------------------------------------


def test_the_backup_never_hides(db_session, groq, event, monkeypatch):
    item = event(title="Tax Seminar")
    _limited(db_session, MAIN)
    _stub(monkeypatch, keep=False)
    ai_categorization.drain_categorization_queue(SessionLocal)
    row = _reload(db_session, item.id)
    assert row.is_active is True
    assert row.ai_label_status == PROVISIONAL


def test_main_labels_overrides_and_hidden_events_are_never_sent_to_the_backup(
    db_session, groq, event, monkeypatch
):
    other = db_session.query(EventCategory).filter_by(slug="other").one()
    current = event(category=other, category_source="ai", ai_prompt_version=PROMPT_VERSION,
                    ai_label_status=LABELED)
    older = event(category=other, category_source="ai", ai_prompt_version="old")
    stale = event(category=other, category_source="ai", ai_prompt_version=PROMPT_VERSION,
                  ai_label_status=STALE)
    overridden = event(category_override_id=other.id)
    hidden = event(is_active=False)
    review = event(ai_label_status=NEEDS_REVIEW, ai_attempts=2)
    _limited(db_session, MAIN)
    calls = _stub(monkeypatch)

    # The backup has nothing it may label, so nothing is sent at all.
    with pytest.raises(GeminiQuotaExceeded):
        ai_categorization.drain_categorization_queue(SessionLocal)
    assert calls == []
    for item in (current, older, stale):
        row = _reload(db_session, item.id)
        assert (row.category_id, row.category_source) == (other.id, "ai")
    assert _reload(db_session, overridden.id).category_override_id == other.id
    assert _reload(db_session, hidden.id).is_active is False
    assert _reload(db_session, review.id).ai_label_status == NEEDS_REVIEW


def test_a_label_or_override_that_lands_mid_request_wins(
    db_session, groq, event, monkeypatch
):
    other = db_session.query(EventCategory).filter_by(slug="other").one()
    labeled = event()
    overridden = event()
    _limited(db_session, MAIN)

    def meanwhile(model):
        with SessionLocal() as db:
            first = db.get(Event, labeled.id)
            first.category_id, first.category_source = other.id, "ai"
            first.ai_prompt_version, first.ai_label_status = PROMPT_VERSION, LABELED
            db.get(Event, overridden.id).category_override_id = other.id
            db.commit()

    _stub(monkeypatch, during=meanwhile)
    assert ai_categorization.drain_categorization_queue(SessionLocal) == 0
    first = _reload(db_session, labeled.id)
    assert (first.category_id, first.category_source, first.ai_model) == (other.id, "ai", None)
    second = _reload(db_session, overridden.id)
    assert second.category_source != PROVISIONAL_SOURCE


# --- Idempotency, failures and retries -----------------------------------------


def test_the_backup_labels_an_event_once(db_session, groq, event, monkeypatch):
    item = event()
    _limited(db_session, MAIN)
    calls = _stub(monkeypatch)
    assert ai_categorization.drain_categorization_queue(SessionLocal) == 1
    with pytest.raises(GeminiQuotaExceeded):
        ai_categorization.drain_categorization_queue(SessionLocal)
    assert calls == [BACKUP]
    assert _reload(db_session, item.id).ai_label_status == PROVISIONAL


@pytest.mark.parametrize("failure", [
    GeminiBadOutput("not JSON"), GeminiTransportError("timed out"),
])
def test_a_failed_backup_request_changes_nothing_visible(
    db_session, groq, event, monkeypatch, failure
):
    item = event()
    before = (item.category_id, item.category_source, item.is_active)
    _limited(db_session, MAIN)
    calls = _stub(monkeypatch, fail={BACKUP: failure})

    # Reported as "wait for the main model", not as an error that backs off.
    with pytest.raises(GeminiQuotaExceeded) as caught:
        ai_categorization.drain_categorization_queue(SessionLocal)
    assert caught.value.retry_after_seconds > 600
    row = _reload(db_session, item.id)
    assert (row.category_id, row.category_source, row.is_active) == before
    assert row.ai_attempts == 0 and row.ai_label_status is None
    # An unusable answer uses up the event's backup try; a timeout does not.
    expected = 1 if isinstance(failure, GeminiBadOutput) else 0
    assert row.ai_fallback_attempts == expected
    assert row in ai_categorization.pending_events(db_session).all()
    with pytest.raises(GeminiQuotaExceeded):
        ai_categorization.drain_categorization_queue(SessionLocal)
    assert calls == [BACKUP] * (1 if expected else 2)


def test_an_unknown_category_from_the_backup_is_not_written(
    db_session, groq, event, monkeypatch
):
    item = event()
    _limited(db_session, MAIN)
    _stub(monkeypatch, category="made-up")
    assert ai_categorization.drain_categorization_queue(SessionLocal) == 0
    row = _reload(db_session, item.id)
    assert row.category_source != PROVISIONAL_SOURCE
    assert (row.ai_attempts, row.ai_fallback_attempts) == (0, 1)


# --- Verification by the main model --------------------------------------------


def test_the_main_model_verifies_provisional_labels_when_its_quota_returns(
    db_session, groq, event, monkeypatch
):
    item = event(title="Tax Seminar")
    _limited(db_session, MAIN)
    calls = _stub(monkeypatch, keep=False)
    ai_categorization.drain_categorization_queue(SessionLocal)
    assert ai_categorization.queue_counts(db_session) == (1, 0)
    status = ai_categorization.queue_status(db_session)
    assert status["provisional"] == 1
    assert status["new_last_24h"] == 1
    assert status["last_24h"]["events_labeled"] == 0
    assert status["last_24h"]["events_provisional"] == 1

    # The main model's wait is over: it is asked again, and its answer is final.
    db_session.query(AiLabelBatch).delete()
    db_session.commit()
    _stub(monkeypatch, keep=False, category="other")
    assert ai_categorization.drain_categorization_queue(SessionLocal) == 1
    row = _reload(db_session, item.id)
    assert row.category.slug == "other"
    assert (row.category_source, row.ai_label_status, row.ai_model) == ("ai", LABELED, MAIN)
    assert row.is_active is False  # the main model may hide
    # Hidden now, so it leaves the queue: nothing waits and nothing is provisional.
    assert ai_categorization.queue_counts(db_session) == (0, 0)
    assert ai_categorization.queue_status(db_session)["provisional"] == 0
    assert calls == [BACKUP]


def test_provisional_events_wait_for_the_main_model_soonest_first(
    db_session, groq, event, monkeypatch
):
    later = event(start_date=SOON + timedelta(days=20))
    sooner = event(start_date=SOON)
    _limited(db_session, MAIN)
    _stub(monkeypatch)
    ai_categorization.drain_categorization_queue(SessionLocal)
    pending = ai_categorization.pending_events(db_session).all()
    assert [e.id for e in pending] == [sooner.id, later.id]
    assert {e.ai_label_status for e in pending} == {PROVISIONAL}


def test_a_rescrape_keeps_a_provisional_label_provisional(db_session, groq, event, monkeypatch):
    item = event()
    _limited(db_session, MAIN)
    _stub(monkeypatch)
    ai_categorization.drain_categorization_queue(SessionLocal)
    row = _reload(db_session, item.id)
    row.description = "New details from the source."
    assert requeue_if_inputs_changed(row) is True
    assert (row.category_source, row.ai_label_status) == (PROVISIONAL_SOURCE, PROVISIONAL)


# --- Admin pages -----------------------------------------------------------------


@pytest.fixture
def admin_client(client, make_super_admin, login):
    make_super_admin(email="fallback-root@example.com")
    login("fallback-root@example.com")
    return client


def test_admin_pages_mark_provisional_labels(
    admin_client, db_session, groq, event, monkeypatch
):
    item = event()
    _limited(db_session, MAIN)
    _stub(monkeypatch)
    ai_categorization.drain_categorization_queue(SessionLocal)

    page = admin_client.get("/admin/ai-labeling")
    assert page.status_code == 200
    assert "Provisional (backup model, not verified)" in page.text
    assert "(backup, provisional)" in page.text
    detail = admin_client.get(f"/admin/events/{item.id}")
    assert detail.status_code == 200
    assert "AI, provisional (backup model)" in detail.text
    assert "not verified yet" in detail.text
