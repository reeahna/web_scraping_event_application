"""The scheduler's background AI-categorization drain. The Gemini client's
classify_batch is stubbed, so no network call is made; only the selection,
application, and enable/disable gating are exercised against the test DB."""

from __future__ import annotations

from app.config import get_settings
from app.database import SessionLocal
from app.models.event import Event
from app.services import ai_categorization
from app.services.ai_categorizer import EventLabel, GeminiCategorizer


def _enable(monkeypatch, **overrides):
    settings = get_settings()
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(settings, "gemini_scheduled_enabled", True)
    monkeypatch.setattr(settings, "gemini_batch_size", 10)
    for key, value in overrides.items():
        monkeypatch.setattr(settings, key, value)


def test_drain_is_disabled_without_a_key(db_session, make_city, make_event, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "gemini_api_key", None)
    make_event(make_city(), title="Jazz Night")
    assert ai_categorization.drain_categorization_queue(SessionLocal) == 0


def test_drain_labels_pending_events(db_session, make_city, make_event, monkeypatch):
    _enable(monkeypatch)
    city = make_city()
    jazz = make_event(city, title="Jazz Night", canonical_url="https://x/1")
    empanada = make_event(city, title="Empanada Cooking Class", canonical_url="https://x/2")
    wanted = {jazz.id: EventLabel("music"), empanada.id: EventLabel("food-and-drink")}

    def fake_classify(self, options, events):
        return {ev.id: wanted[ev.id] for ev in events if ev.id in wanted}

    monkeypatch.setattr(GeminiCategorizer, "classify_batch", fake_classify)

    labeled = ai_categorization.drain_categorization_queue(SessionLocal, limit=10)
    assert labeled == 2

    db_session.expire_all()
    jazz_row = db_session.get(Event, jazz.id)
    empanada_row = db_session.get(Event, empanada.id)
    assert jazz_row.category_source == "ai"
    assert jazz_row.category.slug == "music"
    assert empanada_row.category.slug == "food-and-drink"


def test_drain_skips_already_ai_labeled_events(db_session, make_city, make_event, monkeypatch):
    _enable(monkeypatch)
    city = make_city()
    make_event(city, title="Already Done", category_source="ai")

    calls: list[int] = []

    def fake_classify(self, options, events):
        calls.append(len(events))
        return {}

    monkeypatch.setattr(GeminiCategorizer, "classify_batch", fake_classify)

    # Nothing pending -> no API call is made at all.
    assert ai_categorization.drain_categorization_queue(SessionLocal, limit=10) == 0
    assert calls == []


def test_drain_hides_events_the_model_drops(db_session, make_city, make_event, monkeypatch):
    _enable(monkeypatch)
    city = make_city()
    pottery = make_event(city, title="Intro to Pottery Class", canonical_url="https://x/1")
    finance = make_event(city, title="Retirement Planning Seminar", canonical_url="https://x/2")
    wanted = {
        pottery.id: EventLabel("arts-and-culture"),
        finance.id: EventLabel("business", keep=False),
    }

    def fake_classify(self, options, events):
        return {ev.id: wanted[ev.id] for ev in events if ev.id in wanted}

    monkeypatch.setattr(GeminiCategorizer, "classify_batch", fake_classify)

    assert ai_categorization.drain_categorization_queue(SessionLocal, limit=10) == 2

    db_session.expire_all()
    assert db_session.get(Event, pottery.id).is_active is True
    finance_row = db_session.get(Event, finance.id)
    assert finance_row.is_active is False
    assert finance_row.category.slug == "business"


def test_drain_keeps_dropped_events_when_hiding_is_off(
    db_session, make_city, make_event, monkeypatch
):
    _enable(monkeypatch, gemini_hide_unwanted=False)
    finance = make_event(make_city(), title="Retirement Planning Seminar")

    def fake_classify(self, options, events):
        return {ev.id: EventLabel("business", keep=False) for ev in events}

    monkeypatch.setattr(GeminiCategorizer, "classify_batch", fake_classify)

    assert ai_categorization.drain_categorization_queue(SessionLocal, limit=10) == 1
    db_session.expire_all()
    assert db_session.get(Event, finance.id).is_active is True
