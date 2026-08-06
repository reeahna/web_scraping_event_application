"""Browser-backed structured pagination.

The browser navigates the source page ONCE, then walks every event page from
within the page context via its own fetch() — same origin/cookies/Referer and
the same rendered date window, only the offset cursor changing. These tests use
a fake browser that serves pages by decoding options.skip, so the pagination
policy (stop conditions, dedup, caps, combination) is verified deterministically
without launching a real browser.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qsl, urlencode, urlsplit

from app.extraction.browser import (
    BrowserJsonPage,
    BrowserPagedResult,
    BrowserStructuredResponseFetchStrategy,
)
from app.extraction.types import FetchRequest
from app.schemas.extraction import FetchConfig, SiteConfiguration
from app.schemas.request_recipe import RecipePagination, RecipeValue, RequestRecipe
from app.services.extraction_runs import _execute_pipeline

URL = "https://events.example.org/events/this-weekend/"
API = "https://events.example.org/includes/rest_v2/plugins_events_events_by_date/find/"
TZ = "America/Indiana/Indianapolis"


def _recipe(limit=12, total_path="docs.count"):
    return RequestRecipe(
        method="GET",
        endpoint=API,
        query_params={
            "json": RecipeValue(
                kind="json_template",
                value={
                    "filter": {
                        "date_range": {
                            "start": {"$date": {"kind": "window_start_utc"}},
                            "end": {"$date": {"kind": "window_end_utc"}},
                        }
                    },
                    "options": {"limit": {"kind": "page_limit"}, "skip": {"kind": "page_offset"}},
                },
            ),
            "token": RecipeValue(kind="literal", value="PUB-TOKEN-XYZ"),
        },
        source_page_url=URL,
        pagination=RecipePagination(
            kind="offset", limit=limit, total_path=total_path, max_pages=50
        ),
    )


def _records(n, start=0):
    return [
        {"recid": str(1000 + i), "title": f"Event {i}", "startDate": "2026-10-06",
         "url": f"/event/e{i}/{1000 + i}/"}
        for i in range(start, start + n)
    ]


def _seed_url(limit=12):
    # The page's OWN observed request: exact json (fixed date window + token),
    # skip=0. Pagination reuses this URL and changes only the offset.
    body = {
        "filter": {
            "date_range": {
                "start": {"$date": "2026-08-06T04:00:00.000Z"},
                "end": {"$date": "2026-09-05T03:59:59.999Z"},
            }
        },
        "options": {"limit": limit, "skip": 0},
    }
    return API + "?" + urlencode({"json": json.dumps(body), "token": "PUB-TOKEN-XYZ"})


class _PagingBrowser:
    def __init__(self, records, total, *, limit=12, total_key="count",
                 blocked=None, status=200, repeat_first=False):
        self._records = records
        self._total = total
        self._limit = limit
        self._total_key = total_key
        self._blocked = blocked
        self._status = status
        self._repeat_first = repeat_first
        self.navigated = None
        self.fetched_urls: list[str] = []

    @staticmethod
    def _skip(url):
        q = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
        return int(json.loads(q["json"])["options"]["skip"])

    def _observed(self):
        return [(_seed_url(self._limit), {"docs": {self._total_key: self._total,
                                                   "docs": self._records[0:self._limit]}})]

    async def render_and_fetch_json_pages(self, source_page_url, plan=None, *, next_url, max_pages):
        self.navigated = source_page_url
        if self._blocked is not None:
            return BrowserPagedResult(
                final_url=source_page_url, status_code=self._status, blocked_reason=self._blocked
            )
        observed = self._observed()
        pages: list[BrowserJsonPage] = []
        for _ in range(max_pages):
            url = next_url(pages, observed)
            if not url:
                break
            self.fetched_urls.append(url)
            skip = 0 if self._repeat_first else self._skip(url)
            slice_ = self._records[skip: skip + self._limit]
            body = {"docs": {self._total_key: self._total, "docs": slice_}}
            pages.append(BrowserJsonPage(url=url, status=self._status, json=body))
        return BrowserPagedResult(final_url=source_page_url, status_code=200, pages=pages)


def _strategy(browser, recipe, *, max_pages=50, max_records=5000):
    return BrowserStructuredResponseFetchStrategy(
        source_page_url=URL, endpoint_match=API, browser=browser, recipe=recipe,
        record_path="docs.docs", timezone="UTC", max_pages=max_pages, max_records=max_records,
    )


def _run(strategy):
    resp = asyncio.run(strategy.fetch(FetchRequest(url=API), FetchConfig()))
    body = json.loads(resp.text) if resp.body else {}
    records = body.get("docs", {}).get("docs", []) if isinstance(body, dict) else []
    return resp, records


# --- collection + stop conditions --------------------------------------------


def test_collects_all_pages_until_reported_total():
    browser = _PagingBrowser(_records(108), total=108)
    resp, records = _run(_strategy(browser, _recipe()))
    assert len(records) == 108
    assert resp.pagination["pages_fetched"] == 9
    assert resp.pagination["reported_total"] == 108
    assert resp.pagination["captured_records"] == 108
    assert resp.pagination["unique_records"] == 108
    assert resp.pagination["stop_reason"] == "reported_total_reached"
    assert resp.pagination["offsets"] == [0, 12, 24, 36, 48, 60, 72, 84, 96]


def test_stops_on_short_final_page():
    browser = _PagingBrowser(_records(30), total=None)
    resp, records = _run(_strategy(browser, _recipe()))
    assert len(records) == 30
    assert resp.pagination["stop_reason"] == "short_page"
    assert resp.pagination["offsets"] == [0, 12, 24]


def test_stops_on_empty_page():
    browser = _PagingBrowser(_records(24), total=100)
    resp, records = _run(_strategy(browser, _recipe()))
    assert len(records) == 24
    assert resp.pagination["stop_reason"] == "empty_page"


def test_stops_on_no_new_records():
    browser = _PagingBrowser(_records(12), total=100, repeat_first=True)
    resp, records = _run(_strategy(browser, _recipe()))
    assert len(records) == 12
    assert resp.pagination["stop_reason"] == "no_new_records"


def test_respects_max_pages_cap():
    browser = _PagingBrowser(_records(500), total=500)
    resp, records = _run(_strategy(browser, _recipe(), max_pages=3))
    assert resp.pagination["pages_fetched"] == 3
    assert len(records) == 36
    assert resp.pagination["stop_reason"] == "max_pages"


def test_respects_max_records_cap():
    browser = _PagingBrowser(_records(500), total=500)
    resp, records = _run(_strategy(browser, _recipe(), max_records=40))
    assert len(records) >= 40
    assert resp.pagination["stop_reason"] == "max_records"


def test_supports_docs_total_path():
    browser = _PagingBrowser(_records(48), total=48, total_key="total")
    resp, records = _run(_strategy(browser, _recipe(total_path="docs.total")))
    assert len(records) == 48
    assert resp.pagination["reported_total"] == 48
    assert resp.pagination["stop_reason"] == "reported_total_reached"


def test_stops_on_non_2xx_mid_walk():
    class _Flaky(_PagingBrowser):
        async def render_and_fetch_json_pages(self, src, plan=None, *, next_url, max_pages):
            self.navigated = src
            observed = self._observed()
            pages = []
            for _ in range(max_pages):
                url = next_url(pages, observed)
                if not url:
                    break
                skip = self._skip(url)
                if skip == 0:
                    body = {"docs": {"count": 108, "docs": self._records[0:self._limit]}}
                    pages.append(BrowserJsonPage(url=url, status=200, json=body))
                else:
                    pages.append(BrowserJsonPage(url=url, status=403, json={"error": "denied"}))
            return BrowserPagedResult(final_url=src, status_code=200, pages=pages)

    browser = _Flaky(_records(108), total=108)
    resp, records = _run(_strategy(browser, _recipe()))
    assert len(records) == 12
    assert resp.pagination["stop_reason"] == "non_2xx_status"


# --- request invariants ------------------------------------------------------


def test_only_offset_changes_date_window_and_token_preserved():
    browser = _PagingBrowser(_records(108), total=108)
    _run(_strategy(browser, _recipe()))
    windows, skips, tokens = set(), [], set()
    for url in browser.fetched_urls:
        q = dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))
        j = json.loads(q["json"])
        dr = j["filter"]["date_range"]
        windows.add((dr["start"]["$date"], dr["end"]["$date"]))
        skips.append(j["options"]["skip"])
        tokens.add(q["token"])
    assert len(windows) == 1  # identical rendered date window on every page
    assert tokens == {"PUB-TOKEN-XYZ"}  # token preserved
    assert skips == [0, 12, 24, 36, 48, 60, 72, 84, 96]  # only the offset advances
    assert browser.navigated == URL  # navigated the source page once


def test_blocked_render_passes_through():
    browser = _PagingBrowser([], total=0, blocked="edge_protection:http_403", status=403)
    resp, _ = _run(_strategy(browser, _recipe()))
    assert resp.blocked_reason == "edge_protection:http_403"


# --- shared pipeline (preview == import) -------------------------------------


def test_pipeline_finds_all_paginated_records():
    config = SiteConfiguration(
        pattern_name="simpleview_events", listing_url=URL, api_endpoint=API,
        execution_strategy="browser", timezone=TZ, json_paths={"events_root": "docs.docs"},
        pagination={"strategy": "none", "max_pages": 50, "max_events": 5000},
        max_detail_fetches=0,
    )
    browser = _PagingBrowser(_records(108), total=108)
    outcome = asyncio.run(
        _execute_pipeline(config, config.pattern_name, _strategy(browser, _recipe()),
                          fallback_timezone=TZ)
    )
    assert len(outcome.outcomes) == 108
    assert outcome.last_response.pagination["pages_fetched"] == 9
