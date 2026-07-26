"""Phase 9: the restricted browser fetch strategy and observation.

These tests drive the real headless chromium against a local fixture server —
no external network, no live sites. SSRF normally blocks localhost, so the
tests that use the local server permit its host via the `_host_allowed`
indirection; the SSRF test deliberately leaves that in place and asserts a
private URL is refused.

If chromium cannot launch in the environment, the browser tests skip with a
clear reason rather than fail — but where the binary is present (as here) they
actually run.
"""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, patch

import pytest

from app.extraction.browser import BrowserFetchStrategy
from app.schemas.browser import (
    BrowserPlan,
    ClickAction,
    LoadMoreAction,
    NetworkIdleAction,
    ScrollAction,
    WaitForSelectorAction,
)
from app.services.browser_observation import render_and_observe

# --- pages served by the local fixture server --------------------------------

_CARDS_PAGE = """<!DOCTYPE html><html><head><title>Rendered</title></head><body>
<div id="app"></div>
<script>
  const events = [
    {t:"Autumn Quartet", u:"/e/autumn", d:"October 6, 2026"},
    {t:"Winter Chorus", u:"/e/winter", d:"November 13, 2026"},
    {t:"Spring Recital", u:"/e/spring", d:"March 7, 2027"},
    {t:"Summer Jazz", u:"/e/summer", d:"June 13, 2027"}
  ];
  document.getElementById('app').innerHTML =
    '<div class="event-list">' + events.map(e =>
      '<div class="event-card"><h3 class="event-title"><a href="'+e.u+'">'+e.t+
      '</a></h3><span class="event-date">'+e.d+'</span></div>').join('') + '</div>';
</script></body></html>"""

_ALGOLIA_PAGE = """<!DOCTYPE html><html><head><title>Search</title></head><body>
<div id="results"></div>
<script>
  fetch('/algolia-api').then(r => r.json()).then(d => { window.__loaded = d.nbHits; });
</script></body></html>"""

_ALGOLIA_API = (
    '{"hits":['
    '{"objectID":"a1","title":"Jazz Evening","url":"/e/jazz","start":"2026-10-06T20:00:00"},'
    '{"objectID":"a2","title":"Poetry Slam","url":"/e/poetry","start":"2026-10-13T19:00:00"},'
    '{"objectID":"a3","title":"Craft Fair","url":"/e/craft","start":"2026-10-20T10:00:00"}'
    '],"nbHits":3,"page":0,"nbPages":1}'
)

_CHALLENGE_PAGE = """<!DOCTYPE html><html><body>
<h1>Checking your browser before accessing the site</h1>
<p>Please enable JavaScript and cookies to continue.</p></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence
        pass

    def do_GET(self):  # noqa: N802
        routes = {
            "/cards": ("text/html", _CARDS_PAGE),
            "/algolia-page": ("text/html", _ALGOLIA_PAGE),
            "/algolia-api": ("application/json", _ALGOLIA_API),
            "/challenge": ("text/html", _CHALLENGE_PAGE),
        }
        content_type, body = routes.get(self.path, ("text/html", "<html></html>"))
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()


@pytest.fixture(scope="module")
def browser_available():
    async def _check():
        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                await browser.close()
            return True
        except Exception:  # noqa: BLE001
            return False

    return asyncio.run(_check())


@pytest.fixture
def allow_local():
    """Permit the local fixture host through the SSRF gate (both the initial
    navigation and every subrequest resolve the same module-level hook)."""
    with patch("app.extraction.browser._host_allowed", new=AsyncMock(return_value=True)):
        yield


def _require_browser(browser_available):
    if not browser_available:
        pytest.skip("chromium is not available in this environment")


# --- schema ------------------------------------------------------------------


def test_the_plan_schema_rejects_an_unknown_action():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        BrowserPlan.model_validate({"actions": [{"action": "evaluate", "code": "alert(1)"}]})


def test_the_plan_schema_caps_counts_and_timeouts():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        LoadMoreAction(selector=".more", max_clicks=999)
    with pytest.raises(ValidationError):
        WaitForSelectorAction(selector=".x", timeout_ms=10**9)
    with pytest.raises(ValidationError):
        ScrollAction(max_scrolls=10**6)


def test_a_plan_has_no_field_for_arbitrary_code():
    # There is simply no action that carries a script/expression; the whole
    # surface is the enumerated set.
    fields = set()
    for action in (WaitForSelectorAction, ClickAction, LoadMoreAction, ScrollAction):
        fields |= set(action.model_fields)
    assert not (fields & {"code", "script", "expression", "eval", "js"})


# --- SSRF (no browser needed) ------------------------------------------------


def test_a_private_url_is_refused_before_launch():
    strategy = BrowserFetchStrategy()
    result = asyncio.run(strategy.render("http://127.0.0.1:5/admin"))
    assert result.blocked_reason is not None
    assert result.blocked_reason.startswith("ssrf_blocked")
    assert result.rendered_html == ""


# --- rendering (real browser) ------------------------------------------------


def test_javascript_rendered_cards_are_captured(server, browser_available, allow_local):
    _require_browser(browser_available)
    strategy = BrowserFetchStrategy()
    result = asyncio.run(
        strategy.render(f"{server}/cards", BrowserPlan(actions=[WaitForSelectorAction(
            selector=".event-card")]))
    )
    assert result.blocked_reason is None
    assert "event-card" in result.rendered_html
    assert result.rendered_html.count("event-card") >= 4


def test_observation_detects_a_pattern_in_rendered_html(server, browser_available, allow_local):
    _require_browser(browser_available)
    observation = asyncio.run(
        render_and_observe(
            f"{server}/cards",
            plan=BrowserPlan(actions=[WaitForSelectorAction(selector=".event-card")]),
        )
    )
    assert observation.usable
    assert observation.chosen_source == "rendered_html"
    assert observation.detection.pattern_name == "generic_html_cards"


def test_a_structured_api_is_preferred_over_rendered_html(server, browser_available, allow_local):
    _require_browser(browser_available)
    observation = asyncio.run(
        render_and_observe(
            f"{server}/algolia-page", plan=BrowserPlan(actions=[NetworkIdleAction()])
        )
    )
    # The page's own JSON endpoint detects as Algolia and is chosen over the
    # (event-less) rendered HTML.
    assert observation.chosen_source == "structured_api"
    assert observation.detection.pattern_name == "algolia_search"
    assert observation.api_responses


def test_a_challenge_page_is_reported_blocked_never_solved(server, browser_available, allow_local):
    _require_browser(browser_available)
    result = asyncio.run(BrowserFetchStrategy().render(f"{server}/challenge"))
    assert result.blocked_reason is not None
    assert "challenge" in result.blocked_reason or "login" in result.blocked_reason
    # Nothing was captured — we did not push past the wall.
    assert result.rendered_html == ""


def test_the_browser_is_reusable_after_a_render(server, browser_available, allow_local):
    """A proxy for 'everything was closed cleanly': a second render succeeds,
    so the first left no browser/context wedged open."""
    _require_browser(browser_available)
    strategy = BrowserFetchStrategy()
    plan = BrowserPlan(actions=[NetworkIdleAction()])
    first = asyncio.run(strategy.render(f"{server}/cards", plan))
    second = asyncio.run(strategy.render(f"{server}/cards", plan))
    assert first.blocked_reason is None and second.blocked_reason is None
