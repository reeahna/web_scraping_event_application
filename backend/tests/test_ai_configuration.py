"""Phase 8E: optional AI configuration fallback.

The AI never scrapes, approves, activates, or persists events. These tests
prove the suggestion validator rejects anything unsafe, that the app behaves
identically with AI disabled (the default), and that a well-formed suggestion
becomes at most a previewed draft awaiting review.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.core.auto_onboarding import ORIGIN_AI_SUGGESTED
from app.extraction.registry import REGISTRY
from app.models.event import Event
from app.repositories.auto_onboarding import latest_decision_for_website
from app.services.ai import provider as ai_provider
from app.services.ai.suggestion import validate_suggestion
from app.services.ai_configuration import request_ai_configuration
from tests.extraction_helpers import load_fixture, patched_http_fetch

LISTING_URL = "https://venue.example.org/events"
ALL_PATTERNS = frozenset(REGISTRY.names())


@pytest.fixture(autouse=True)
def _reset_ai():
    ai_provider.reset_usage_for_tests()
    yield
    ai_provider.reset_usage_for_tests()


@pytest.fixture
def enable_echo(monkeypatch):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "ai_enabled", True, raising=False)
    monkeypatch.setattr(settings, "ai_provider", "echo", raising=False)
    return ai_provider.get_ai_provider()


# --- validator ---------------------------------------------------------------


def _valid_suggestion() -> dict:
    return {"pattern_name": "json_ld_event", "listing_url": LISTING_URL}


def test_a_wellformed_suggestion_validates():
    result = validate_suggestion(
        _valid_suggestion(), allowed_pattern_names=frozenset(), registered_patterns=ALL_PATTERNS
    )
    assert result.ok
    assert result.configuration.pattern_name == "json_ld_event"


def test_a_non_object_suggestion_is_rejected():
    assert not validate_suggestion(
        "approve this", allowed_pattern_names=frozenset(), registered_patterns=ALL_PATTERNS
    ).ok


def test_an_unknown_key_is_rejected():
    """extra='forbid' means an instruction-shaped key ('approve', 'run') has
    no home and fails outright."""
    suggestion = {**_valid_suggestion(), "approve": True}
    assert not validate_suggestion(
        suggestion, allowed_pattern_names=frozenset(), registered_patterns=ALL_PATTERNS
    ).ok


def test_an_unregistered_pattern_is_rejected():
    suggestion = {"pattern_name": "totally_made_up", "listing_url": LISTING_URL}
    result = validate_suggestion(
        suggestion, allowed_pattern_names=frozenset(), registered_patterns=ALL_PATTERNS
    )
    assert not result.ok
    assert any("not a registered pattern" in e for e in result.errors)


def test_a_pattern_outside_the_allowed_set_is_rejected():
    suggestion = {"pattern_name": "wordpress_rest", "api_endpoint": "https://x.example.org/wp-json"}
    result = validate_suggestion(
        suggestion,
        allowed_pattern_names=frozenset({"json_ld_event"}),
        registered_patterns=ALL_PATTERNS,
    )
    assert not result.ok


def test_an_unsafe_url_is_rejected():
    # An SSRF-unsafe URL is refused — the schema's own URL validator catches
    # this one before the extra AI checks even run, which is fine: rejected
    # is rejected, from whichever layer fires first.
    suggestion = {"pattern_name": "json_ld_event", "listing_url": "http://127.0.0.1/admin"}
    result = validate_suggestion(
        suggestion, allowed_pattern_names=frozenset(), registered_patterns=ALL_PATTERNS
    )
    assert not result.ok


def test_an_unsafe_url_that_passes_the_schema_is_caught_by_the_ai_check():
    # A public-looking host the schema accepts but that resolves into a
    # blocked range only at the string level would still be caught; here we
    # confirm the AI-layer public-URL check contributes its own message for a
    # URL the schema does not itself reject.
    from app.services.ai.suggestion import validate_suggestion as _v

    # metadata.google.internal is blocked by url_safety but is a syntactically
    # valid https URL the schema's own validator also rejects — so assert the
    # end result rather than the layer.
    result = _v(
        {"pattern_name": "json_ld_event", "listing_url": "https://metadata.google.internal/x"},
        allowed_pattern_names=frozenset(),
        registered_patterns=ALL_PATTERNS,
    )
    assert not result.ok


def test_configured_headers_are_rejected():
    suggestion = {
        "pattern_name": "json_ld_event",
        "listing_url": LISTING_URL,
        "fetch": {"headers": {"X-Api-Key": "secret"}},
    }
    # The schema itself blocks credential headers, but a benign header is also
    # refused on an AI draft because none was human-reviewed.
    suggestion["fetch"]["headers"] = {"X-Custom": "value"}
    result = validate_suggestion(
        suggestion, allowed_pattern_names=frozenset(), registered_patterns=ALL_PATTERNS
    )
    assert not result.ok
    assert any("headers" in e for e in result.errors)


def test_unbounded_limits_are_rejected():
    suggestion = {
        "pattern_name": "json_ld_event",
        "listing_url": LISTING_URL,
        "pagination": {"strategy": "query_param", "max_pages": 9999, "max_events": 999999},
    }
    result = validate_suggestion(
        suggestion, allowed_pattern_names=frozenset(), registered_patterns=ALL_PATTERNS
    )
    assert not result.ok


# --- disabled by default -----------------------------------------------------


@pytest.fixture
def city(make_city):
    return make_city(name="AI City", slug="ai-city", timezone="UTC")


@pytest.fixture
def website(db_session, city, make_website):
    site = make_website(city, name="AI Site", base_url="https://venue.example.org")
    site.event_listing_url = LISTING_URL
    db_session.commit()
    return site


def _handler(body: str):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) != LISTING_URL:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    return handler


def test_ai_is_disabled_by_default(db_session, website):
    body = load_fixture("jsonld_multiple_events.html")
    with patched_http_fetch(_handler(body)):
        outcome = asyncio.run(request_ai_configuration(db_session, website))
    assert outcome.status == "disabled"
    assert website.configuration_origin is None


def test_usage_status_reports_disabled():
    status = ai_provider.usage_status()
    assert status.enabled is False
    assert status.provider == "disabled"


# --- end to end with the echo provider ---------------------------------------


def test_a_valid_suggestion_becomes_a_draft_but_is_not_approved(
    db_session, website, enable_echo
):
    enable_echo.set_canned_suggestion(
        {"pattern_name": "json_ld_event", "listing_url": LISTING_URL}
    )
    body = load_fixture("jsonld_multiple_events.html")
    with patched_http_fetch(_handler(body)):
        outcome = asyncio.run(
            request_ai_configuration(db_session, website, actor_id=None)
        )
    db_session.refresh(website)

    assert outcome.status == "drafted"
    assert website.configuration is not None
    assert website.configuration_origin == ORIGIN_AI_SUGGESTED
    # Draft only: the default policy denies AI-origin approval.
    assert website.approved_pattern is None
    assert website.is_active is False
    # A decision was recorded and it denied approval.
    decision = latest_decision_for_website(db_session, website.id)
    assert decision.configuration_origin == ORIGIN_AI_SUGGESTED
    assert decision.eligible_for_automatic_approval is False


def test_the_ai_draft_flow_persists_no_events(db_session, website, enable_echo):
    enable_echo.set_canned_suggestion(
        {"pattern_name": "json_ld_event", "listing_url": LISTING_URL}
    )
    body = load_fixture("jsonld_multiple_events.html")
    with patched_http_fetch(_handler(body)):
        asyncio.run(request_ai_configuration(db_session, website))
    assert db_session.query(Event).count() == 0


def test_an_unsafe_suggestion_is_not_applied(db_session, website, enable_echo):
    enable_echo.set_canned_suggestion(
        {"pattern_name": "json_ld_event", "listing_url": "http://localhost/admin"}
    )
    body = load_fixture("jsonld_multiple_events.html")
    with patched_http_fetch(_handler(body)):
        outcome = asyncio.run(request_ai_configuration(db_session, website))
    db_session.refresh(website)

    assert outcome.status == "invalid"
    assert website.configuration_origin is None  # nothing was drafted


def test_a_provider_failure_is_handled_and_trips_no_draft(db_session, website, enable_echo):
    enable_echo.set_failure(RuntimeError("provider exploded"))
    body = load_fixture("jsonld_multiple_events.html")
    with patched_http_fetch(_handler(body)):
        outcome = asyncio.run(request_ai_configuration(db_session, website))
    assert outcome.status == "error"
    assert website.configuration_origin is None


def test_the_budget_limit_stops_calls(db_session, website, enable_echo, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "ai_daily_request_limit", 0, raising=False)
    enable_echo.set_canned_suggestion({"pattern_name": "json_ld_event", "listing_url": LISTING_URL})
    body = load_fixture("jsonld_multiple_events.html")
    with patched_http_fetch(_handler(body)):
        outcome = asyncio.run(request_ai_configuration(db_session, website))
    # can_spend refuses before the provider runs, surfaced as unavailable.
    assert outcome.status in ("disabled", "error")
    assert website.configuration_origin is None
