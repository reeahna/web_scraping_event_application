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
    RECOVERY_STRUCTURED_PATTERN_NEEDED,
    RECOVERY_UNSUPPORTED,
    browser_retry_recovery,
)

from .extraction_helpers import (
    blocked_handler,
    html_handler,
    load_fixture,
    patched_http_fetch,
)


def _preview_count(db, website_id) -> int:
    from app.repositories.extraction_run import list_extraction_runs_for_website

    return sum(
        1 for r in list_extraction_runs_for_website(db, website_id, limit=50)
        if r.run_type == "preview"
    )

# A first-party event API in a shape NO registered pattern knows (not docs.docs,
# not algolia hits, etc.) — event-like enough to score as a candidate but
# unextractable, so it exercises the structured_pattern_needed path.
_UNKNOWN_EVENT_API = {
    "results": {
        "items": [
            {"title": "Autumn Quartet", "startDate": "2026-10-06",
             "url": "/event/autumn/123/", "id": "123", "location": "Buskirk"},
            {"title": "Winter Chorus", "startDate": "2026-11-13",
             "url": "/event/winter/124/", "id": "124", "location": "Ivy Tech"},
        ],
    }
}
_FIND_URL = "https://example.com/api/events/search/?token=SECRET"
_TELEMETRY = [
    ("https://pixel.spotify.com/v1/track", {"ok": 1}),
    ("https://ct.pinterest.com/v3/", {"ok": 1}),
    ("https://maps.googleapis.com/maps/api/js/x", {"ok": 1}),
]
_SPA_SHELL = "<html><body><div id='app'></div></body></html>"

LISTING_URL = "https://example.com/events"


# --- fake browser strategies -------------------------------------------------


class _FakeStrategy:
    def __init__(self, result: BrowserRenderResult):
        self._result = result
        self.calls = 0

    async def render(self, url, plan=None):
        self.calls += 1
        return self._result

    async def render_and_fetch_json_pages(self, source_page_url, plan=None, *, next_url, max_pages):
        # Serve the observed structured response(s) as pages so browser
        # structured previews (which always paginate) work with the fake.
        from app.extraction.browser import BrowserJsonPage, BrowserPagedResult

        self.calls += 1
        r = self._result
        if r.blocked_reason is not None:
            return BrowserPagedResult(
                final_url=r.final_url, status_code=r.status_code, blocked_reason=r.blocked_reason
            )
        observed = list(r.observed_json)
        pages = []
        for _ in range(max_pages):
            url = next_url(pages, observed)
            if not url:
                break
            payload = next((p for (u, p) in observed if u == url), None)
            pages.append(BrowserJsonPage(url=url, status=200, json=payload))
        return BrowserPagedResult(final_url=r.final_url, status_code=r.status_code, pages=pages)


def _blocked(reason: str) -> _FakeStrategy:
    return _FakeStrategy(
        BrowserRenderResult(
            final_url=LISTING_URL, rendered_html="", status_code=0, blocked_reason=reason
        )
    )


def _rendered(html: str, observed_json=None, observed_requests=None) -> _FakeStrategy:
    return _FakeStrategy(
        BrowserRenderResult(
            final_url=LISTING_URL,
            rendered_html=html,
            status_code=200,
            observed_json=observed_json or [],
            observed_requests=observed_requests or {},
        )
    )


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def enable_browser(monkeypatch):
    monkeypatch.setattr(get_settings(), "browser_extraction_enabled", True, raising=False)


@pytest.fixture
def disable_browser(monkeypatch):
    # Explicit, so the test is hermetic regardless of any local .env default.
    monkeypatch.setattr(get_settings(), "browser_extraction_enabled", False, raising=False)


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


async def test_browser_retry_refused_when_disabled(db_session, unsupported_site, disable_browser):
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


# --- structured-source preference over rendered HTML ------------------------


async def test_unextractable_first_party_endpoint_routes_to_review(
    db_session, unsupported_site, enable_browser
):
    """The Website #4 case: a first-party event API is discovered but no
    registered pattern can extract it. The outcome is a distinct
    structured_pattern_needed — never a rendered-HTML/generic_html_cards
    proposal — and it touches no draft, preview, or configuration_version."""
    website = unsupported_site()
    starting_version = website.configuration_version
    observed = [(_FIND_URL, _UNKNOWN_EVENT_API), *_TELEMETRY]
    result = await browser_retry_recovery(
        db_session, website, strategy=_rendered(_SPA_SHELL, observed_json=observed)
    )

    assert result.status == RECOVERY_STRUCTURED_PATTERN_NEEDED
    # Rendered HTML is not selected and its detection is not exposed.
    assert result.observation.chosen_source is None
    assert result.observation.detection is None
    db_session.refresh(website)
    assert website.onboarding_status == "needs_review"
    assert website.configuration is None  # no draft created
    assert website.configuration_version == starting_version  # version unchanged
    assert _preview_count(db_session, website.id) == 0  # no preview run
    assert _event_count(db_session) == 0

    rec = db_session.query(UnsupportedSiteReport).one().browser_recovery
    assert rec["status"] == RECOVERY_STRUCTURED_PATTERN_NEEDED
    assert rec["new_pattern_needed"] is True
    assert rec["chosen_source"] is None
    assert rec["proposed_pattern"] is None
    assert rec["preview_status"] is None
    assert rec["selected_endpoint"].endswith("/search/")
    candidates = rec["candidate_event_endpoints"]
    assert len(candidates) == 1
    assert candidates[0]["record_array_path"] == "results.items"
    # Query strings and secrets never persisted.
    assert "SECRET" not in json.dumps(rec)
    assert "token" not in candidates[0]["url"]
    # Third-party telemetry ignored, never presented as a candidate.
    assert rec["ignored_endpoint_count"] == 3
    assert rec["rejected_candidates"]


async def test_proposer_not_called_for_structured_pattern_needed(
    db_session, unsupported_site, enable_browser, monkeypatch
):
    from app.services import browser_recovery

    def _boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("save_draft_configuration must not be called")

    monkeypatch.setattr(browser_recovery, "save_draft_configuration", _boom)
    website = unsupported_site()
    result = await browser_retry_recovery(
        db_session, website,
        strategy=_rendered(_SPA_SHELL, observed_json=[(_FIND_URL, _UNKNOWN_EVENT_API)]),
    )
    assert result.status == RECOVERY_STRUCTURED_PATTERN_NEEDED


async def test_structured_pattern_needed_retries_are_idempotent(
    db_session, unsupported_site, enable_browser
):
    website = unsupported_site()
    observed = [(_FIND_URL, _UNKNOWN_EVENT_API), *_TELEMETRY]

    await browser_retry_recovery(
        db_session, website, strategy=_rendered(_SPA_SHELL, observed_json=observed)
    )
    db_session.refresh(website)
    version_after_first = website.configuration_version
    first = db_session.query(UnsupportedSiteReport).one().browser_recovery
    assert first["attempts"] == 1

    # An equivalent retry must not bump the version, create a preview, or add a
    # duplicate report — only the attempt counter advances.
    await browser_retry_recovery(
        db_session, website, strategy=_rendered(_SPA_SHELL, observed_json=observed)
    )
    db_session.refresh(website)
    assert website.configuration_version == version_after_first
    assert _preview_count(db_session, website.id) == 0
    assert db_session.query(UnsupportedSiteReport).count() == 1
    second = db_session.query(UnsupportedSiteReport).one().browser_recovery
    assert second["attempts"] == 2
    assert second["candidate_fingerprint"] == first["candidate_fingerprint"]


async def test_production_orchestration_prefers_structured_over_generic(
    db_session, unsupported_site, enable_browser
):
    """find JSON (high score) + aggregate JSON + rendered HTML that matches
    generic_html_cards + no pattern for find. The structured candidate wins;
    generic_html_cards never drafts or previews."""
    website = unsupported_site()
    starting_version = website.configuration_version
    cards = load_fixture("static_html_cards.html")  # would match generic_html_cards
    observed = [
        (_FIND_URL, _UNKNOWN_EVENT_API),
        ("https://example.com/includes/rest_v2/plugins_events_events/aggregate/",
         {"aggregations": {"categories": {"Music": 10}}, "total": 3}),
    ]
    result = await browser_retry_recovery(
        db_session, website, strategy=_rendered(cards, observed_json=observed)
    )

    assert result.status == RECOVERY_STRUCTURED_PATTERN_NEEDED
    db_session.refresh(website)
    rec = db_session.query(UnsupportedSiteReport).one().browser_recovery
    assert rec["selected_endpoint"].endswith("/search/")
    assert rec["chosen_source"] is None  # rendered HTML did not win
    assert rec["proposed_pattern"] is None
    assert website.configuration is None
    assert website.configuration_version == starting_version
    assert _preview_count(db_session, website.id) == 0
    assert _event_count(db_session) == 0
    # The aggregate endpoint is not a candidate (below threshold).
    assert len(rec["candidate_event_endpoints"]) == 1


async def test_extractable_structured_endpoint_beats_rendered_html(
    db_session, unsupported_site, enable_browser
):
    website = unsupported_site()
    cards = load_fixture("static_html_cards.html")
    observed = [(
        "https://example.com/1/indexes/events/query",
        json.loads(load_fixture("algolia_response.json")),
    )]
    await browser_retry_recovery(
        db_session, website, strategy=_rendered(cards, observed_json=observed)
    )

    rec = db_session.query(UnsupportedSiteReport).one().browser_recovery
    # The extractable structured API wins over rendered generic_html_cards.
    assert rec["chosen_source"] == "structured_api"
    assert rec["detected_pattern"] == "algolia_search"
    assert rec["new_pattern_needed"] is False


async def test_new_draft_supersedes_prior_failed_version(
    db_session, unsupported_site, enable_browser
):
    website = unsupported_site()
    # Simulate the historical failed generic_html_cards draft (version 1).
    website.configuration = {"pattern_name": "generic_html_cards", "listing_url": LISTING_URL}
    website.configuration_version = 1
    db_session.add(website)
    db_session.commit()

    cards = load_fixture("static_html_cards.html")
    with patched_http_fetch(html_handler("static_html_cards.html")):
        await browser_retry_recovery(db_session, website, strategy=_rendered(cards))

    db_session.refresh(website)
    # A new version supersedes v1; the new preview references the new version.
    assert website.configuration_version > 1
    from app.repositories.extraction_run import list_extraction_runs_for_website

    preview = next(
        r for r in list_extraction_runs_for_website(db_session, website.id, limit=20)
        if r.run_type == "preview"
    )
    assert preview.configuration_version == website.configuration_version


async def test_browser_recovery_selects_simpleview_and_drafts_next_version(
    db_session, unsupported_site, enable_browser
):
    """With the pattern registered, an observed Simpleview find response is now
    extractable: recovery selects simpleview_events, drafts a new configuration
    version (preserving the prior failed generic version), and previews it —
    no rendered_html/generic_html_cards, no Event rows."""
    website = unsupported_site()
    website.timezone_override = "America/Indiana/Indianapolis"
    # Simulate the historical failed generic_html_cards draft at version 5.
    website.configuration = {"pattern_name": "generic_html_cards", "listing_url": LISTING_URL}
    website.configuration_version = 5
    db_session.add(website)
    db_session.commit()

    find_url = "https://example.com/includes/rest_v2/plugins_events_events_by_date/find/?token=X"
    find_payload = json.loads(load_fixture("simpleview_events_page1.json"))
    strategy = _rendered(
        _SPA_SHELL,
        observed_json=[(find_url, find_payload)],
        observed_requests={find_url: {"method": "GET", "query_param_names": ["token"],
                                      "response_status": 200}},
    )
    # Preview re-fetches the (query-stripped) endpoint over HTTP.
    with patched_http_fetch(html_handler("simpleview_events_page1.json", "application/json")):
        result = await browser_retry_recovery(db_session, website, strategy=strategy)

    db_session.refresh(website)
    assert result.observation.chosen_source == "structured_api"
    assert result.observation.detection.pattern_name == "simpleview_events"
    # A new draft version supersedes v5; v5 is preserved (not mutated).
    assert website.configuration_version == 6
    assert website.configuration["pattern_name"] == "simpleview_events"
    assert website.approved_pattern is None  # policy off — never auto-approved
    assert website.is_active is False
    assert _event_count(db_session) == 0

    from app.repositories.extraction_run import list_extraction_runs_for_website

    preview = next(
        r for r in list_extraction_runs_for_website(db_session, website.id, limit=20)
        if r.run_type == "preview"
    )
    assert preview.configuration_version == 6

    rec = db_session.query(UnsupportedSiteReport).one().browser_recovery
    assert rec["new_pattern_needed"] is False
    assert rec["chosen_source"] == "structured_api"
    assert rec["proposed_pattern"] == "simpleview_events"
    assert rec["record_array_path"] == "docs.docs"
    assert rec["source_id_field"] == "recid"
    assert rec["request_method"] == "GET"
    # Query names recorded, token value never persisted anywhere.
    assert "token" not in json.dumps(website.configuration)


# --- registry-driven selector metadata --------------------------------------


def test_pattern_options_cover_the_registry_without_hardcoding():
    options = pattern_options(REGISTRY)
    names = [o["name"] for o in options]
    assert set(names) == set(REGISTRY.names())
    assert len(names) == 12
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


# --- fresh onboarding: browser fallback with no prior Website history --------

_SIMPLEVIEW_FIND = "https://example.com/includes/rest_v2/plugins_events_events_by_date/find/"


def _simpleview_observed(n: int = 12) -> dict:
    records = [
        {
            "recid": str(1000 + i), "title": f"Community Event {i}",
            "startDate": "2026-10-06", "url": f"/event/e{i}/{1000 + i}/", "location": "Downtown",
        }
        for i in range(n)
    ]
    return {"docs": {"count": n, "docs": records}}


def _fresh_strategy(observed, warnings=()):
    return _FakeStrategy(
        BrowserRenderResult(
            final_url=LISTING_URL, rendered_html=_SPA_SHELL, status_code=200,
            observed_json=observed, warnings=tuple(warnings),
        )
    )


def _browser_preview_run(db, website):
    from app.repositories.extraction_run import list_extraction_runs_for_website

    runs = [
        r
        for r in list_extraction_runs_for_website(db, website.id, limit=50)
        if r.run_type == "preview"
    ]
    # Newest preview run (recovery previews HTTP first, then the browser successor).
    return max(runs, key=lambda r: r.id) if runs else None


async def test_fresh_website_browser_fallback_on_plain_http_403(
    db_session, unsupported_site, enable_browser
):
    """The regression: a brand-new Website (no config, no recovery history)
    whose HTTP replay returns a PLAIN http_403 (not a recognised edge
    signature) must still get a browser successor in the SAME recovery run."""
    website = unsupported_site()
    assert website.configuration is None  # fresh: no prior configuration

    find = _SIMPLEVIEW_FIND + "?json=%7B%7D&token=PUBTOKEN"
    observed = [
        (find, _simpleview_observed(12)),
        ("https://bs.serving-sys.com/pixel", {"ad": True}),  # unrelated 3rd-party
    ]
    strategy = _fresh_strategy(observed, warnings=("blocked_subrequest:bs.serving-sys.com",))

    # Plain 403 with no edge/WAF body markers -> classified as http_403.
    with patched_http_fetch(blocked_handler(403, "Forbidden")):
        result = await browser_retry_recovery(db_session, website, strategy=strategy)

    db_session.refresh(website)
    cfg = website.configuration
    assert cfg is not None
    # The current configuration is the browser successor.
    assert cfg["execution_strategy"] == "browser"
    assert cfg["pattern_name"] == "simpleview_events"
    assert cfg["json_paths"]["events_root"] == "docs.docs"
    assert cfg["listing_url"] == LISTING_URL
    assert cfg["request_recipe"] is None
    # One HTTP (blocked, historical) + one browser (current) version — the fresh
    # Website started at 0, so the browser successor is v2.
    assert website.configuration_version == 2
    # Browser preview captured the records and succeeded.
    run = _browser_preview_run(db_session, website)
    assert run.status in ("success", "partial")
    assert run.events_found == 12
    assert run.events_valid == 12
    assert result.status == READY_FOR_APPROVAL


async def test_ignored_ad_subrequest_does_not_block_fresh_recovery(
    db_session, unsupported_site, enable_browser
):
    # The blocked third-party ad subrequest is only a diagnostic warning — it
    # must not set the recovery outcome to blocked when the first-party event
    # endpoint was captured successfully.
    website = unsupported_site()
    observed = [(_SIMPLEVIEW_FIND + "?token=X", _simpleview_observed(12))]
    strategy = _fresh_strategy(observed, warnings=("blocked_subrequest:bs.serving-sys.com",))
    with patched_http_fetch(blocked_handler(403, "Forbidden")):
        result = await browser_retry_recovery(db_session, website, strategy=strategy)
    assert result.status == READY_FOR_APPROVAL
    db_session.refresh(website)
    assert website.configuration["execution_strategy"] == "browser"


async def test_repeated_fresh_recovery_creates_no_equivalent_http_draft(
    db_session, unsupported_site, enable_browser
):
    # §8: once browser is proven, a second recovery must not create another
    # equivalent HTTP draft first (nor a duplicate browser version).
    website = unsupported_site()
    observed = [(_SIMPLEVIEW_FIND + "?token=X", _simpleview_observed(12))]

    def _run():
        return browser_retry_recovery(
            db_session, website, strategy=_fresh_strategy(observed)
        )

    with patched_http_fetch(blocked_handler(403, "Forbidden")):
        await _run()
    db_session.refresh(website)
    version_after_first = website.configuration_version  # 2 (http v1 + browser v2)
    assert website.configuration["execution_strategy"] == "browser"

    with patched_http_fetch(blocked_handler(403, "Forbidden")):
        await _run()
    db_session.refresh(website)
    # No new version: the current config was already the browser successor.
    assert website.configuration_version == version_after_first
    assert website.configuration["execution_strategy"] == "browser"


async def test_selected_endpoint_failure_still_blocks(
    db_session, unsupported_site, enable_browser
):
    # A genuine browser render block (challenge/SSRF on the page itself) is NOT
    # turned into a browser success — recovery reports blocked as before.
    website = unsupported_site()
    result = await browser_retry_recovery(
        db_session, website, strategy=_blocked("challenge_marker:captcha")
    )
    assert result.status == RECOVERY_BLOCKED


async def test_observation_prefers_structured_rendered_over_unextractable_api():
    """When the only observed API candidate can't be extracted by any pattern
    but the rendered page carries a robust *structured* pattern (schema.org
    JSON-LD), the observation selects the rendered structured source instead of
    declaring structured_pattern_needed."""
    from app.services.browser_observation import (
        OUTCOME_RENDERED_SELECTED,
        render_and_observe,
    )

    itemlist_html = (
        '<html><head><script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"ItemList","itemListElement":['
        '{"@type":"ListItem","item":{"@type":"Event","name":"E1",'
        '"startDate":"2026-09-01","url":"https://example.com/e/1"}}]}'
        "</script></head><body></body></html>"
    )
    strategy = _rendered(itemlist_html, observed_json=[(_FIND_URL, _UNKNOWN_EVENT_API)])
    obs = await render_and_observe(LISTING_URL, strategy=strategy)

    assert obs.outcome == OUTCOME_RENDERED_SELECTED
    assert obs.chosen_source == "rendered_html"
    assert obs.detection.pattern_name == "json_ld_event"


async def test_observation_still_needs_pattern_when_rendered_is_only_static():
    """The refinement is narrow: a *static* (HTML-card) rendered pattern does
    NOT override a real-but-unextractable API candidate — that still asks for a
    reusable pattern rather than falling back to fragile scraping."""
    from app.services.browser_observation import (
        OUTCOME_STRUCTURED_PATTERN_NEEDED,
        render_and_observe,
    )

    cards = (
        '<html><body><div class="event-card"><h3><a href="/e/x">X</a></h3>'
        '<time datetime="2026-09-01">Sep 1</time></div></body></html>'
    )
    strategy = _rendered(cards, observed_json=[(_FIND_URL, _UNKNOWN_EVENT_API)])
    obs = await render_and_observe(LISTING_URL, strategy=strategy)

    assert obs.outcome == OUTCOME_STRUCTURED_PATTERN_NEEDED
