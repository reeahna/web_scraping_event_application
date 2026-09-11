"""Gemini event categorizer: prompt building, response parsing, and the slug
validation that keeps a hallucinated category from ever being applied. No test
touches the network — the HTTP POST is stubbed."""

from __future__ import annotations

import json

import pytest

from app.services.ai_categorizer import (
    CategoryOption,
    EventToClassify,
    GeminiCategorizer,
    GeminiError,
    GeminiUnavailable,
    _parse_labels,
    build_prompt,
)

CATEGORIES = [
    CategoryOption("music", "Music", "Concerts and live music"),
    CategoryOption("food-and-drink", "Food and drink", "Tastings, dinners, classes"),
    CategoryOption("education", "Education", "Classes and lectures"),
    CategoryOption("other", "Other"),
]


def _categorizer() -> GeminiCategorizer:
    # A real key value, but every test stubs _post so nothing is sent.
    return GeminiCategorizer("test-key", "gemini-2.5-flash", min_interval_seconds=0.0)


def _response(items: list[dict]) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": json.dumps(items)}]}}]}


def test_missing_key_is_unavailable():
    with pytest.raises(GeminiUnavailable):
        GeminiCategorizer("", "gemini-2.5-flash")


def test_prompt_lists_slugs_and_events():
    prompt = build_prompt(
        CATEGORIES, [EventToClassify(id=7, title="Empanada Cooking Class")]
    )
    assert "food-and-drink" in prompt
    assert "Empanada Cooking Class" in prompt
    # The cooking-class-is-food steer must be present.
    assert "cooking class" in prompt.lower()


def test_parse_labels_plain_array():
    assert _parse_labels('[{"id": 1, "category": "music"}]') == {1: "music"}


def test_parse_labels_tolerates_code_fence():
    text = '```json\n[{"id": 2, "category": "education"}]\n```'
    assert _parse_labels(text) == {2: "education"}


def test_parse_labels_drops_malformed_entries():
    text = '[{"id": 1, "category": "music"}, {"no_id": true}, {"id": "x"}]'
    assert _parse_labels(text) == {1: "music"}


def test_classify_batch_applies_valid_slugs(monkeypatch):
    categorizer = _categorizer()
    events = [
        EventToClassify(id=1, title="Jazz Night"),
        EventToClassify(id=2, title="Empanada Cooking Class"),
    ]
    monkeypatch.setattr(
        categorizer,
        "_post",
        lambda prompt: _response(
            [{"id": 1, "category": "music"}, {"id": 2, "category": "food-and-drink"}]
        ),
    )
    assert categorizer.classify_batch(CATEGORIES, events) == {
        1: "music",
        2: "food-and-drink",
    }


def test_classify_batch_drops_unknown_slug_and_unrequested_id(monkeypatch):
    categorizer = _categorizer()
    events = [EventToClassify(id=1, title="Jazz Night")]
    monkeypatch.setattr(
        categorizer,
        "_post",
        lambda prompt: _response(
            [
                {"id": 1, "category": "wizardry"},  # not a real slug -> dropped
                {"id": 99, "category": "music"},  # not requested -> dropped
            ]
        ),
    )
    assert categorizer.classify_batch(CATEGORIES, events) == {}


def test_classify_batch_empty_events_makes_no_call():
    categorizer = _categorizer()
    # No _post stub: if it tried to call out, httpx would fail. It must not.
    assert categorizer.classify_batch(CATEGORIES, []) == {}


def test_no_candidates_raises(monkeypatch):
    categorizer = _categorizer()
    monkeypatch.setattr(categorizer, "_post", lambda prompt: {"promptFeedback": {}})
    with pytest.raises(GeminiError):
        categorizer.classify_batch(CATEGORIES, [EventToClassify(id=1, title="x")])
