"""Unit coverage for the recovery browser-fallback decision (§2/§3).

These prove the pure decision helpers that make recovery propose a browser
configuration when an HTTP replay is edge-blocked, and refuse to persist a
duplicate. The full end-to-end recovery (real browser + preview) is exercised
by the live manual verification, not here.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

from app.schemas.extraction import SiteConfiguration
from app.services.browser_recovery import (
    _browser_structured_config,
    _preview_edge_blocked,
    _same_recovery_config,
    _structured_endpoint_config,
)

API = "https://events.example.org/includes/rest_v2/plugins_events_events_by_date/find/"
PAGE = "https://events.example.org/events/this-weekend/"


def _preview(status: str, warnings: list[str]):
    return NS(result=NS(status=status, warnings=tuple(warnings)))


def _http_simpleview_config() -> SiteConfiguration:
    return SiteConfiguration(
        pattern_name="simpleview_events",
        api_endpoint=API,
        json_paths={"events_root": "docs.docs"},
        request_recipe={
            "method": "GET", "endpoint": API,
            "query_params": {"token": {"kind": "literal", "value": "PUBTOKEN"}},
        },
    )


# --- edge-block detection ----------------------------------------------------


def test_edge_block_detected_only_for_edge_protection():
    assert _preview_edge_blocked(_preview("blocked", ["edge_protection:http_403", "…"])) is True
    # A plain block (not edge protection) does not trigger the browser fallback.
    assert _preview_edge_blocked(_preview("blocked", ["http_403"])) is False
    # A successful/failed preview never triggers it.
    assert _preview_edge_blocked(_preview("success", [])) is False
    assert _preview_edge_blocked(_preview("failed", ["parse_error"])) is False


# --- structured-endpoint eligibility -----------------------------------------


def test_structured_endpoint_config_recognises_structured_pattern():
    assert _structured_endpoint_config(_http_simpleview_config()) is True


def test_structured_endpoint_config_rejects_html_and_endpointless():
    html = SiteConfiguration(
        pattern_name="generic_html_cards", listing_url=PAGE,
        event_container_selector=".item",
    )
    assert _structured_endpoint_config(html) is False
    no_endpoint = SiteConfiguration(pattern_name="simpleview_events", listing_url=PAGE)
    assert _structured_endpoint_config(no_endpoint) is False


# --- rebuild as a browser structured config ----------------------------------


def test_browser_structured_config_switches_transport_and_drops_recipe():
    browser = _browser_structured_config(_http_simpleview_config(), source_page_url=PAGE)
    assert browser.execution_strategy == "browser"
    assert browser.listing_url == PAGE  # navigate the source page, not the API
    assert browser.request_recipe is None  # no HTTP replay
    # Extraction is unchanged: same pattern, endpoint to capture, record path.
    assert browser.pattern_name == "simpleview_events"
    assert browser.api_endpoint == API
    assert browser.json_paths == {"events_root": "docs.docs"}


# --- duplicate prevention ----------------------------------------------------


def test_same_recovery_config_true_for_equivalent():
    browser = _browser_structured_config(_http_simpleview_config(), source_page_url=PAGE)
    existing = browser.model_dump(mode="json")
    assert _same_recovery_config(existing, browser) is True


def test_same_recovery_config_false_when_strategy_differs():
    http = _http_simpleview_config()
    browser = _browser_structured_config(http, source_page_url=PAGE)
    # An HTTP version is materially different from the browser version.
    assert _same_recovery_config(http.model_dump(mode="json"), browser) is False


def test_same_recovery_config_false_for_missing_previous():
    browser = _browser_structured_config(_http_simpleview_config(), source_page_url=PAGE)
    assert _same_recovery_config(None, browser) is False
