"""Groq as the categorization provider: request shape, response parsing,
rate-limit hints, and provider selection. No test touches the network."""

from __future__ import annotations

import json

import httpx

from app.config import get_settings
from app.services import ai_categorization
from app.services.ai_categorizer import (
    CategoryOption,
    EventLabel,
    EventToClassify,
    GeminiCategorizer,
    GroqCategorizer,
    _parse_labels,
    _retry_after_seconds,
)

CATEGORIES = [CategoryOption("music", "Music"), CategoryOption("other", "Other")]


def test_payload_is_openai_style_json_mode():
    categorizer = GroqCategorizer("k", "llama-3.3-70b-versatile")
    payload = categorizer._payload("PROMPT")
    assert payload["model"] == "llama-3.3-70b-versatile"
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["messages"][-1] == {"role": "user", "content": "PROMPT"}
    assert categorizer._client.headers["authorization"] == "Bearer k"


def test_classify_batch_reads_wrapped_labels(monkeypatch):
    categorizer = GroqCategorizer("k", "m", min_interval_seconds=0.0)
    content = json.dumps({"labels": [{"id": 1, "category": "music", "keep": False}]})
    monkeypatch.setattr(
        categorizer, "_post", lambda prompt: {"choices": [{"message": {"content": content}}]}
    )
    labels = categorizer.classify_batch(CATEGORIES, [EventToClassify(id=1, title="Jazz")])
    assert labels == {1: EventLabel("music", keep=False)}


def test_parse_labels_still_accepts_a_bare_array():
    assert _parse_labels('[{"id": 2, "category": "other"}]') == {2: EventLabel("other")}


def test_retry_after_from_groq_header_and_message():
    assert _retry_after_seconds(httpx.Response(429, headers={"retry-after": "42"})) == 42
    body = {"error": {"message": "Rate limit reached. Please try again in 7m12.5s."}}
    assert _retry_after_seconds(httpx.Response(429, json=body)) == 432.5


def _keys(monkeypatch, *, groq=None, gemini=None):
    settings = get_settings()
    monkeypatch.setattr(settings, "groq_api_key", groq)
    monkeypatch.setattr(settings, "gemini_api_key", gemini)
    return settings


def test_groq_is_preferred_when_its_key_is_set(monkeypatch):
    settings = _keys(monkeypatch, groq="g", gemini="x")
    assert ai_categorization.provider_name() == "groq"
    assert ai_categorization.batch_size() == settings.groq_batch_size
    with ai_categorization.make_categorizer() as categorizer:
        assert isinstance(categorizer, GroqCategorizer)


def test_gemini_is_used_without_a_groq_key(monkeypatch):
    settings = _keys(monkeypatch, gemini="x")
    assert ai_categorization.provider_name() == "gemini"
    assert ai_categorization.batch_size() == settings.gemini_batch_size
    with ai_categorization.make_categorizer() as categorizer:
        assert type(categorizer) is GeminiCategorizer


def test_no_key_means_no_provider(monkeypatch):
    _keys(monkeypatch)
    assert ai_categorization.provider_name() is None
    assert ai_categorization.scheduled_categorization_enabled() is False


def test_a_groq_key_alone_enables_scheduled_labeling(monkeypatch):
    settings = _keys(monkeypatch, groq="g")
    monkeypatch.setattr(settings, "gemini_scheduled_enabled", True)
    assert ai_categorization.scheduled_categorization_enabled() is True
