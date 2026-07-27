"""Phase 17: optional AI event enrichment (mocked provider only)."""

from __future__ import annotations

import pytest

from app.services.ai.provider import reset_usage_for_tests
from app.services.ai_enrichment import (
    DisabledEnrichmentProvider,
    EchoEnrichmentProvider,
    EnrichmentSuggestion,
    enrich_event,
    is_unresolved,
    mark_enrichment,
    summarize_extraction_errors,
)


@pytest.fixture(autouse=True)
def _reset_ai():
    reset_usage_for_tests()
    yield
    reset_usage_for_tests()


_counter = iter(range(1, 10_000))


def _uncategorized_event(make_city, make_event, **over):
    n = next(_counter)
    city = make_city(name=f"City {n}", slug=f"city-{n}")
    over.setdefault("canonical_url", f"https://x/e{n}")
    return make_event(city, title="Mystery Show", description="A show.", **over)


def _echo(canned=None, failure=None):
    provider = EchoEnrichmentProvider()
    if canned is not None:
        provider.set_canned(canned)
    if failure is not None:
        provider.set_failure(failure)
    return provider


def test_disabled_provider_produces_nothing(make_city, make_event, db_session):
    event = _uncategorized_event(make_city, make_event)
    assert enrich_event(db_session, event, provider=DisabledEnrichmentProvider()) is None


def test_echo_suggestion_is_stored(make_city, make_event, db_session):
    event = _uncategorized_event(make_city, make_event)  # "Music" is a seeded category
    provider = _echo({"category_suggestion": "Music", "tags": ["jazz", "live"],
                      "summary": "A jazz show.", "family_friendly": True})
    result = enrich_event(db_session, event, provider=provider, taxonomy={"Music"})
    assert result is not None
    assert result.category_suggestion == "Music"
    assert result.tags == ["jazz", "live"]
    assert result.family_friendly is True
    assert result.status == "suggested"


def test_result_is_cached(make_city, make_event, db_session):
    event = _uncategorized_event(make_city, make_event)
    first = enrich_event(db_session, event, provider=_echo({"summary": "S"}), taxonomy=set())
    assert first is not None
    # A now-failing provider still returns the cached row (cache checked first).
    cached = enrich_event(
        db_session, event, provider=_echo(failure=RuntimeError("boom")), taxonomy=set()
    )
    assert cached is not None
    assert cached.id == first.id


def test_hallucinated_category_is_dropped(make_city, make_event, db_session):
    event = _uncategorized_event(make_city, make_event)
    provider = _echo({"category_suggestion": "Totally Made Up"})
    result = enrich_event(db_session, event, provider=provider, taxonomy={"Music", "Theater"})
    assert result is not None
    assert result.category_suggestion is None  # not in taxonomy -> dropped


def test_resolved_events_are_never_sent(make_city, make_event, make_category, db_session):
    category = make_category(name="Custom Cat", slug="custom-cat")
    city = make_city(name="Resolved City", slug="resolved-city")
    event = make_event(city, title="Has Category", category=category)
    assert is_unresolved(event) is False
    assert enrich_event(db_session, event, provider=_echo({"summary": "x"})) is None


def test_authoritative_fields_are_never_touched(make_city, make_event, db_session):
    from datetime import date
    event = _uncategorized_event(make_city, make_event, start_date=date(2026, 9, 1),
                                 venue="Real Venue")
    enrich_event(
        db_session, event,
        provider=_echo({"summary": "s", "category_suggestion": "X", "tags": ["a"]}),
        taxonomy=set(),
    )
    db_session.refresh(event)
    assert event.start_date == date(2026, 9, 1)
    assert event.venue == "Real Venue"
    assert event.category_id is None  # suggestion never auto-applied


def test_ai_failure_returns_none_and_trips_circuit(make_city, make_event, db_session):
    # Enough consecutive failures open the circuit; the next call is blocked
    # before the provider is even consulted.
    for i in range(5):
        event = _uncategorized_event(make_city, make_event,
                                     canonical_url=f"https://x/{i}")
        assert enrich_event(
            db_session, event, provider=_echo(failure=RuntimeError("boom")), taxonomy=set()
        ) is None
    # Circuit now open: a healthy provider is not even called.
    healthy = _echo({"summary": "would work"})
    blocked_event = _uncategorized_event(make_city, make_event, canonical_url="https://x/final")
    assert enrich_event(db_session, blocked_event, provider=healthy, taxonomy=set()) is None


def test_extraction_error_summary(db_session):
    assert summarize_extraction_errors([], provider=_echo({"summary": "x"})) is None
    assert summarize_extraction_errors(["boom"], provider=DisabledEnrichmentProvider()) is None
    text = summarize_extraction_errors(["boom", "bang"], provider=_echo({"summary": "Two errors."}))
    assert text == "Two errors."


def test_suggestion_schema_bounds_tags():
    s = EnrichmentSuggestion.model_validate({"tags": ["a", " b ", ""] + ["x"] * 20})
    assert len(s.tags) <= 8
    assert "b" in s.tags


def test_mark_enrichment(make_city, make_event, db_session):
    event = _uncategorized_event(make_city, make_event)
    result = enrich_event(db_session, event, provider=_echo({"summary": "s"}), taxonomy=set())
    mark_enrichment(db_session, result, "applied")
    db_session.refresh(result)
    assert result.status == "applied"
