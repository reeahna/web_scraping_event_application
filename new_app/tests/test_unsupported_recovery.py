"""Unsupported-source recovery: registry-driven selector, detector-explanation
honesty, and restricted-browser retry.

The browser-retry tests never launch a real browser — they pass a fake fetch
strategy (a stand-in returning a canned render) into the recovery service, so
they run fast and deterministically. Preview's ordinary HTTP fetch is served by
the shared MockTransport helper, so no live network is touched.
"""

from __future__ import annotations

import json

import pytest

from app.config import get_settings
from app.core.exceptions import AppError
from app.extraction.browser import BrowserRenderResult
from app.extraction.inference.policy import READY_FOR_APPROVAL
from app.extraction.registry import REGISTRY, pattern_options
from app.models.event import Event
from app.models.unsupported_site_report import UnsupportedSiteReport
from app.services.browser_recovery import (
    RECOVERY_BLOCKED,
    RECOVERY_UNSUPPORTED,
    browser_retry_recovery,
)

from .extraction_helpers import html_handler, load_fixture, patched_http_fetch

LISTING_URL = "https://example.com/events"


# --- fake browser strategies -------------------------------------------------


class _FakeStrategy:
    def __init__(self, result: BrowserRenderResult):
        self._result = result
        self.calls = 0

    async def render(self, url, plan=None):
        self.calls += 1
        return self._result


def _blocked(reason: str) -> _FakeStrategy:
    return _FakeStrategy(
        BrowserRenderResult(
            final_url=LISTING_URL, rendered_html="", status_code=0, blocked_reason=reason
        )
    )


def _rendered(html: str, observed_json=None) -> _FakeStrategy:
    return _FakeStrategy(
        BrowserRenderResult(
            final_url=LISTING_URL,
            rendered_html=html,
            status_code=200,
            observed_json=observed_json or [],
        )
    )


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def enable_browser(monkeypatch):
    monkeypatch.setattr(get_settings(), "browser_extraction_enabled", True, raising=False)


@pytest.fixture
def unsupported_site(db_session, make_city, make_website):
    def _make(*, archived: bool = False, status: str = "unsupported"):
        city = make_city()
        website = make_website(city, archived=archived)
        website.onboarding_status = status
        website.event_listing_url = LISTING_URL
        db_session.add(website)
        db_session.commit()
        report = UnsupportedSiteReport(
            website_id=website.id,
            submitted_url=LISTING_URL,
            fingerprint="fp-recovery",
            failure_reason="no_pattern_matched",
        )
        db_session.add(report)
        db_session.commit()
        db_session.refresh(website)
        return website

    return _make


def _event_count(db) -> int:
    return db.query(Event).count()


# --- guards ------------------------------------------------------------------


async def test_browser_retry_refused_when_disabled(db_session, unsupported_site):
    website = unsupported_site()
    with pytest.raises(AppError) as exc:
        await browser_retry_recovery(db_session, website, strategy=_rendered("<html></html>"))
    assert exc.value.status_code == 409


async def test_archived_website_cannot_retry(db_session, unsupported_site, enable_browser):
    website = unsupported_site(archived=True)
    with pytest.raises(AppError) as exc:
        await browser_retry_recovery(db_session, website, strategy=_rendered("<html></html>"))
    assert exc.value.status_code == 409


# --- blocked outcomes --------------------------------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        "challenge_marker:captcha",
        "challenge_marker:cloudflare",
        "login_wall:please log in",
        "http_403",
        "ssrf_blocked:internal.host",
        "browser_error:TimeoutError",
    ],
)
async def test_blocked_reasons_are_recorded_and_leave_source_unsupported(
    db_session, unsupported_site, enable_browser, reason
):
    website = unsupported_site()
    result = await browser_retry_recovery(db_session, website, strategy=_blocked(reason))

    assert result.status == RECOVERY_BLOCKED
    db_session.refresh(website)
    assert website.onboarding_status == "unsupported"
    assert website.configuration is None
    assert _event_count(db_session) == 0

    report = db_session.query(UnsupportedSiteReport).one()
    assert report.browser_recovery["status"] == "blocked"
    assert report.browser_recovery["blocked_reason"] == reason
    assert report.browser_recovery["proposed_pattern"] is None


# --- rendered but nothing detected ------------------------------------------


async def test_rendered_without_pattern_stays_unsupported(
    db_session, unsupported_site, enable_browser
):
    website = unsupported_site()
    result = await browser_retry_recovery(
        db_session, website, strategy=_rendered("<html><body><p>no events here</p></body></html>")
    )

    assert result.status == RECOVERY_UNSUPPORTED
    db_session.refresh(website)
    assert website.configuration is None
    assert _event_count(db_session) == 0
    report = db_session.query(UnsupportedSiteReport).one()
    assert report.browser_recovery["status"] == "unsupported"
    assert "rendered_html" in report.browser_recovery["observed_response_types"]


# --- structured endpoint preferred ------------------------------------------


async def test_structured_endpoint_is_preferred_and_recorded(
    db_session, unsupported_site, enable_browser
):
    website = unsupported_site()
    api_url = "https://example.com/1/indexes/events/query?x-algolia-key=SECRET"
    observed = [(api_url, json.loads(load_fixture("algolia_response.json")))]
    strategy = _rendered("<html><body></body></html>", observed_json=observed)
    await browser_retry_recovery(db_session, website, strategy=strategy)

    report = db_session.query(UnsupportedSiteReport).one()
    recovery = report.browser_recovery
    assert recovery["chosen_source"] == "structured_api"
    assert recovery["detected_pattern"] == "algolia_search"
    # Endpoint recorded without its query string, so no key/token is stored.
    assert recovery["discovered_endpoints"] == ["https://example.com/1/indexes/events/query"]
    assert "x-algolia-key" not in json.dumps(recovery)
    # Algolia cannot be fully inferred from the sample alone, so it lands in
    # review rather than fabricating a configuration.
    db_session.refresh(website)
    assert website.onboarding_status == "needs_review"
    assert _event_count(db_session) == 0


# --- existing pattern recovery: full pipeline through preview ---------------


async def test_rendered_cards_recover_through_preview(
    db_session, unsupported_site, enable_browser
):
    website = unsupported_site()
    cards = load_fixture("static_html_cards.html")
    # Preview re-fetches the listing over HTTP; serve the same rendered markup.
    with patched_http_fetch(html_handler("static_html_cards.html")):
        result = await browser_retry_recovery(db_session, website, strategy=_rendered(cards))

    db_session.refresh(website)
    assert result.status == READY_FOR_APPROVAL
    assert website.onboarding_status == "needs_review"
    # A draft configuration was written (version bumped) but never approved or
    # activated — the seeded policy leaves auto-approval off.
    assert website.configuration is not None
    assert website.configuration["pattern_name"] == "generic_html_cards"
    assert website.configuration_version >= 1
    assert website.approved_pattern is None
    assert website.is_active is False
    # Preview persists no events.
    assert _event_count(db_session) == 0

    report = db_session.query(UnsupportedSiteReport).one()
    assert report.browser_recovery["proposed_pattern"] == "generic_html_cards"
    assert report.browser_recovery["preview_status"] in ("success", "partial")


async def test_recovery_preview_is_version_matched(db_session, unsupported_site, enable_browser):
    website = unsupported_site()
    cards = load_fixture("static_html_cards.html")
    with patched_http_fetch(html_handler("static_html_cards.html")):
        await browser_retry_recovery(db_session, website, strategy=_rendered(cards))

    from app.repositories.extraction_run import list_extraction_runs_for_website

    runs = list_extraction_runs_for_website(db_session, website.id, limit=20)
    preview = next(r for r in runs if r.run_type == "preview")
    # Stale-preview protection: the preview run is stamped with the draft's
    # current configuration version.
    db_session.refresh(website)
    assert preview.configuration_version == website.configuration_version


# --- registry-driven selector metadata --------------------------------------


def test_pattern_options_cover_the_registry_without_hardcoding():
    options = pattern_options(REGISTRY)
    names = [o["name"] for o in options]
    assert set(names) == set(REGISTRY.names())
    assert len(names) == 11
    assert "wordpress_rest" in names
    # Not preselected / not privileged: first by reliability order is the most
    # specific structured pattern, never wordpress_rest by default.
    assert names[0] != "wordpress_rest"
    # Every option carries the metadata the selector renders.
    for option in options:
        assert option["display_name"]
        assert option["classification"] in ("structured", "static", "browser")
        assert option["has_proposer"] is True


def test_pattern_options_flag_supporting_evidence():
    evidence = {"json_ld_event": {"confidence": 0.82}, "wordpress_rest": {"confidence": 0.0}}
    options = {o["name"]: o for o in pattern_options(REGISTRY, evidence)}
    assert options["json_ld_event"]["has_evidence"] is True
    assert options["json_ld_event"]["evidence_confidence"] == 0.82
    assert options["wordpress_rest"]["has_evidence"] is False
    assert options["ics_calendar"]["has_evidence"] is False
