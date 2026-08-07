"""Browser-rendered HTML import parity with the legacy Simpleview scraper.

The legacy scraper rendered the events page in a real browser and parsed
`.item[data-type='events']` DOM cards (title / mini-date-container / thumb),
because the events are injected client-side by JS that calls an edge-protected
API. This ports that behavior generally: execution_strategy="browser" renders the page
via the restricted browser and hands the HTML to the generic_html_cards
pattern. Nothing here is site-specific — selectors and formats are config data.
"""

from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import patch

import pytest

from app.extraction.browser import BrowserRenderFetchStrategy, BrowserRenderResult
from app.extraction.types import FetchRequest
from app.schemas.extraction import FetchConfig, SiteConfiguration
from app.services.extraction_runs import (
    _browser_plan_for,
    _build_fetch_strategy,
    _DisabledBrowserFetchStrategy,
    _execute_pipeline,
)

from .extraction_helpers import load_fixture

TZ = "America/Indiana/Indianapolis"
URL = "https://events.example.org/events/this-weekend/"


class _FakeBrowser:
    """Stands in for the real Playwright browser: returns fixed rendered HTML
    without launching anything, so the render→extract→normalize path is tested
    deterministically."""

    def __init__(self, html: str, *, status: int = 200, blocked: str | None = None):
        self._html = html
        self._status = status
        self._blocked = blocked
        self.last_plan = None

    async def render(self, url: str, plan=None) -> BrowserRenderResult:
        self.last_plan = plan
        return BrowserRenderResult(
            final_url=url, rendered_html=self._html, status_code=self._status,
            blocked_reason=self._blocked,
        )


def _card_config() -> SiteConfiguration:
    return SiteConfiguration(
        pattern_name="generic_html_cards",
        listing_url=URL,
        timezone=TZ,
        execution_strategy="browser",
        event_container_selector=".item[data-type='events']",
        field_selectors={
            "title": {"kind": "css", "selector": "a.title"},
            "canonical_url": {"kind": "attribute", "selector": "a.title", "attribute": "href"},
            "start_datetime": {"kind": "css", "selector": ".mini-date-container"},
            "image": {"kind": "attribute", "selector": "img.thumb", "attribute": "src"},
        },
        transformations=[
            {
                "field": "start_datetime",
                "kind": "parse_date",
                # Year-less card dates ("Aug 07") resolve to the next occurrence
                # from a fixed reference so the test is deterministic.
                "params": {
                    "formats": ["%b %d"],
                    "assume_next_occurrence": True,
                    "reference_date": "2026-08-06",
                },
            }
        ],
        pagination={"strategy": "none"},
        required_fields=["title", "start_date", "canonical_url"],
    )


def _run(config, fetch):
    return asyncio.run(
        _execute_pipeline(config, config.pattern_name, fetch, fallback_timezone=TZ)
    )


# --- the adapter -------------------------------------------------------------


def test_browser_adapter_wraps_render_as_html_fetch_response():
    fake = _FakeBrowser("<html><body><p>hi</p></body></html>")
    strategy = BrowserRenderFetchStrategy(browser=fake)
    resp = asyncio.run(strategy.fetch(FetchRequest(url=URL), FetchConfig()))
    assert resp.status_code == 200
    assert resp.content_type == "text/html"
    assert "hi" in resp.text
    assert resp.blocked_reason is None


def test_browser_adapter_passes_through_block_reason():
    fake = _FakeBrowser("", status=403, blocked="edge_protection:http_403")
    resp = asyncio.run(
        BrowserRenderFetchStrategy(browser=fake).fetch(FetchRequest(url=URL), FetchConfig())
    )
    assert resp.blocked_reason == "edge_protection:http_403"


def test_browser_adapter_flags_empty_render():
    resp = asyncio.run(
        BrowserRenderFetchStrategy(browser=_FakeBrowser("")).fetch(
            FetchRequest(url=URL), FetchConfig()
        )
    )
    assert resp.blocked_reason == "browser_render_empty"


def test_browser_plan_waits_for_container():
    plan = _browser_plan_for(_card_config())
    actions = [a.action for a in plan.actions]
    assert "network_idle" in actions
    assert "wait_for_selector" in actions
    wait = next(a for a in plan.actions if a.action == "wait_for_selector")
    assert wait.selector == ".item[data-type='events']"


# --- strategy selection ------------------------------------------------------


def _settings(browser_enabled: bool):
    class _S:
        browser_extraction_enabled = browser_enabled

    return _S()


def test_strategy_selection_http_by_default():
    from app.extraction.fetch import HttpFetchStrategy

    config = _card_config().model_copy(update={"execution_strategy": "http"})
    assert isinstance(_build_fetch_strategy(config), HttpFetchStrategy)


def test_strategy_selection_browser_when_enabled():
    with patch("app.services.extraction_runs.get_settings", return_value=_settings(True)):
        assert isinstance(_build_fetch_strategy(_card_config()), BrowserRenderFetchStrategy)


def test_strategy_selection_blocked_when_browser_disabled():
    with patch("app.services.extraction_runs.get_settings", return_value=_settings(False)):
        strategy = _build_fetch_strategy(_card_config())
    assert isinstance(strategy, _DisabledBrowserFetchStrategy)
    resp = asyncio.run(strategy.fetch(FetchRequest(url=URL), FetchConfig()))
    assert resp.blocked_reason == "browser_rendering_disabled"


# --- full pipeline parity ----------------------------------------------------


def test_rendered_cards_pipeline_matches_legacy_extraction():
    html = load_fixture("simpleview_rendered_cards.html")
    config = _card_config()
    fetch = BrowserRenderFetchStrategy(browser=_FakeBrowser(html), plan=_browser_plan_for(config))
    outcome = _run(config, fetch)

    valid = [(c, r) for c, r in outcome.outcomes if r.is_valid]
    # Three event cards (the trailing data-type="promo" card is not matched).
    assert len(outcome.outcomes) == 3
    assert len(valid) == 3  # nonzero found AND valid

    by_title = {c.title: c for c, _ in valid}
    assert set(by_title) == {
        "Riverside Food Truck Friday", "Gem and Mineral Show", "Downtown Gallery Walk",
    }
    # Year inference: Aug dates land in the reference year, the Jan date rolls to
    # next year (it is earlier in the year than the reference).
    assert by_title["Riverside Food Truck Friday"].start_date == date(2026, 8, 7)
    assert by_title["Gem and Mineral Show"].start_date == date(2026, 8, 8)
    assert by_title["Downtown Gallery Walk"].start_date == date(2027, 1, 5)
    # Relative card URLs resolve against the rendered page.
    assert by_title["Riverside Food Truck Friday"].canonical_url == (
        "https://events.example.org/event/riverside-food-truck-friday/1001/"
    )
    # Timezone applied; HTTP status is the rendered page's.
    assert by_title["Gem and Mineral Show"].timezone == TZ
    assert outcome.last_response.status_code == 200
    assert outcome.last_response.blocked_reason is None


def test_rendered_cards_blocked_render_yields_no_events():
    config = _card_config()
    fetch = BrowserRenderFetchStrategy(
        browser=_FakeBrowser("", status=403, blocked="edge_protection:http_403")
    )
    outcome = _run(config, fetch)
    assert len(outcome.outcomes) == 0
    assert outcome.last_response.blocked_reason == "edge_protection:http_403"


# --- structured-response browser extraction (approach B) ---------------------

API = "https://www.visitbloomington.com/includes/rest_v2/plugins_events_events_by_date/find/"
PAGE = "https://events.example.org/events/this-weekend/"


class _FakeObservingBrowser:
    """Returns pre-baked observed JSON responses (as the real render captures
    from the page's own XHRs) without launching a browser. Records the URL it
    was asked to navigate to."""

    def __init__(self, observed, *, status: int = 200, blocked: str | None = None):
        self._observed = observed
        self._status = status
        self._blocked = blocked
        self.navigated_to = None

    async def render(self, url: str, plan=None) -> BrowserRenderResult:
        self.navigated_to = url
        return BrowserRenderResult(
            final_url=url, rendered_html="<html></html>", status_code=self._status,
            observed_json=self._observed, blocked_reason=self._blocked,
        )

    async def render_and_fetch_json_pages(self, source_page_url, plan=None, *, next_url, max_pages):
        from app.extraction.browser import BrowserJsonPage, BrowserPagedResult

        self.navigated_to = source_page_url
        if self._blocked is not None:
            return BrowserPagedResult(
                final_url=source_page_url, status_code=self._status, blocked_reason=self._blocked
            )
        pages = []
        for _ in range(max_pages):
            url = next_url(pages, self._observed)
            if not url:
                break
            payload = next((p for (u, p) in self._observed if u == url), None)
            pages.append(BrowserJsonPage(url=url, status=self._status, json=payload))
        return BrowserPagedResult(
            final_url=source_page_url, status_code=self._status, pages=pages
        )


def _sv_payload(n: int) -> dict:
    records = [
        {
            "recid": str(1000 + i), "title": f"Event {i}",
            "startDate": "2026-10-06", "url": f"/event/e{i}/{1000 + i}/",
        }
        for i in range(n)
    ]
    return {"docs": {"count": n, "docs": records}}


def _structured_config() -> SiteConfiguration:
    return SiteConfiguration(
        pattern_name="simpleview_events", listing_url=PAGE, api_endpoint=API,
        execution_strategy="browser", timezone=TZ,
        event_container_selector=".item[data-type='events']",
        json_paths={"events_root": "docs.docs"},
        pagination={"strategy": "none"}, max_detail_fetches=0,
    )


def _structured_strategy(browser):
    from app.extraction.browser import BrowserStructuredResponseFetchStrategy

    return BrowserStructuredResponseFetchStrategy(
        source_page_url=PAGE, endpoint_match=API, browser=browser
    )


def test_structured_strategy_captures_endpoint_and_rejects_telemetry():
    observed = [
        ("https://pixels.spotify.com/v1/ingest", {"response": "ok"}),
        ("https://www.google-analytics.com/g/collect?tid=1", {"x": 1}),
        (API + "?json=%7B%7D&token=PUBTOKEN", _sv_payload(3)),
    ]
    browser = _FakeObservingBrowser(observed)
    resp = asyncio.run(_structured_strategy(browser).fetch(FetchRequest(url=API), FetchConfig()))
    # Navigated to the *source page*, never the API directly.
    assert browser.navigated_to == PAGE
    assert resp.content_type == "application/json"
    body = __import__("json").loads(resp.text)
    assert list(body["docs"]["docs"][0]) == ["recid", "title", "startDate", "url"]
    assert resp.blocked_reason is None


def test_structured_strategy_no_matching_response_is_honest():
    observed = [("https://www.google-analytics.com/g/collect", {"x": 1})]
    resp = asyncio.run(
        _structured_strategy(_FakeObservingBrowser(observed)).fetch(
            FetchRequest(url=API), FetchConfig()
        )
    )
    assert resp.blocked_reason == "browser_no_structured_response"
    # Never surfaces a spurious http_403 from a path that was not taken.
    assert "403" not in (resp.blocked_reason or "")


def test_structured_strategy_passes_through_block():
    browser = _FakeObservingBrowser([], blocked="edge_protection:http_403", status=403)
    resp = asyncio.run(_structured_strategy(browser).fetch(FetchRequest(url=API), FetchConfig()))
    assert resp.blocked_reason == "edge_protection:http_403"


def test_structured_pipeline_extracts_docs_docs_twelve_records():
    config = _structured_config()
    browser = _FakeObservingBrowser([(API + "?token=X", _sv_payload(12))])
    fetch = _structured_strategy(browser)
    outcome = _run(config, fetch)
    valid = [(c, r) for c, r in outcome.outcomes if r.is_valid]
    assert len(outcome.outcomes) == 12  # all docs.docs records reached normalization
    assert len(valid) == 12
    assert outcome.last_response.content_type == "application/json"
    assert outcome.last_response.final_url.split("?")[0] == API
    # Browser preview applies the (DST-aware IANA) timezone consistently.
    assert all(c.timezone == TZ for c, _ in valid)


def test_fallback_timezone_uses_city_iana_when_no_override():
    from types import SimpleNamespace as NS

    from app.services.extraction_runs import _fallback_timezone

    city = NS(timezone="America/Indiana/Indianapolis")
    # No override -> the city's DST-aware IANA zone (not a bare EST abbreviation).
    assert _fallback_timezone(NS(timezone_override=None, city=city)) == (
        "America/Indiana/Indianapolis"
    )
    # An explicit override is still honored.
    assert _fallback_timezone(NS(timezone_override="America/New_York", city=city)) == (
        "America/New_York"
    )


def test_strategy_selection_structured_browser():
    from app.extraction.browser import BrowserStructuredResponseFetchStrategy

    with patch("app.services.extraction_runs.get_settings", return_value=_settings(True)):
        strategy = _build_fetch_strategy(_structured_config())
    assert isinstance(strategy, BrowserStructuredResponseFetchStrategy)


def test_preview_browser_config_does_not_call_http_recipe_executor():
    # A browser config must never hit the HTTP RequestRecipe executor.
    config = _structured_config()
    browser = _FakeObservingBrowser([(API + "?token=X", _sv_payload(3))])
    with patch("app.services.extraction_runs.render_recipe") as recipe_spy:
        _run(config, _structured_strategy(browser))
    recipe_spy.assert_not_called()


# --- browser safety properties (source-level, no live browser) ---------------


def _browser_source() -> str:
    import inspect

    import app.extraction.browser as mod

    return inspect.getsource(mod)


def test_response_listeners_registered_before_navigation():
    src = _browser_source()
    # Guards (which register the response listener) are installed before the
    # first navigation, so the event XHR is never missed.
    assert src.index("_install_guards") < src.index("page.goto")


def test_browser_context_closed_in_finally_and_no_persistent_state():
    src = _browser_source()
    assert "finally:" in src
    assert "_safe_close_context" in src and "_safe_close_browser" in src
    # No persistent profile / stored cookies between runs.
    assert "storage_state" not in src
    assert "user_data_dir" not in src


def test_browser_plan_timeouts_are_bounded():
    # The closed plan schema caps every wait, so a config cannot ask the browser
    # to wait indefinitely.
    import pydantic

    from app.schemas.browser import WaitForSelectorAction

    with pytest.raises(pydantic.ValidationError):
        WaitForSelectorAction(selector=".x", timeout_ms=10_000_000)


# --- year-inference transform ------------------------------------------------


@pytest.mark.parametrize(
    "text,reference,expected",
    [
        ("Aug 07", "2026-08-06", date(2026, 8, 7)),  # upcoming this year
        ("Aug 06", "2026-08-06", date(2026, 8, 6)),  # today counts as upcoming
        ("Jan 05", "2026-08-06", date(2027, 1, 5)),  # already past -> next year
        ("Dec 31", "2026-08-06", date(2026, 12, 31)),  # end-of-year, still upcoming
    ],
)
def test_parse_date_next_occurrence(text, reference, expected):
    from app.extraction.transform import _parse_date

    params = {"formats": ["%b %d"], "assume_next_occurrence": True, "reference_date": reference}
    assert _parse_date(text, params) == expected


def test_parse_date_inference_is_opt_in():
    from app.extraction.transform import _parse_date

    # Without the flag, a year-less date keeps strptime's default year (unchanged
    # behavior for every existing config).
    assert _parse_date("Aug 07", {"formats": ["%b %d"]}) == date(1900, 8, 7)


def test_parse_date_does_not_clobber_real_year():
    from app.extraction.transform import _parse_date

    params = {
        "formats": ["%b %d %Y"],
        "assume_next_occurrence": True,
        "reference_date": "2026-08-06",
    }
    assert _parse_date("Aug 07 2030", params) == date(2030, 8, 7)
