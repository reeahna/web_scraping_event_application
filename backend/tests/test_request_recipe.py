"""Captured request recipes: schema, capture/normalization, dynamic date
windows (incl. DST), rendering, redaction, and end-to-end execution through the
shared preview/import pipeline.

Everything here is provider-agnostic. The one Simpleview-shaped fixture
(`simpleview_find_captured_request.json`) uses a generic example domain and a
placeholder token — it exercises the *shape* of a structured request, never a
real site or credential.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx
import pydantic
import pytest

from app.extraction.request_recipe import (
    capture_recipe,
    redact_token,
    render_recipe,
    summarize_recipe,
)
from app.schemas.request_recipe import (
    RecipePagination,
    RecipeValue,
    RecipeWindow,
    RequestRecipe,
)

from .extraction_helpers import FIXTURES_DIR, patched_http_fetch

# --- helpers -----------------------------------------------------------------


def _fixture() -> dict:
    return json.loads(
        (FIXTURES_DIR / "simpleview_find_captured_request.json").read_text(encoding="utf-8")
    )


def _captured_url(fx: dict) -> str:
    return fx["endpoint"] + "?" + urlencode(
        {"json": json.dumps(fx["query_json"]), "token": fx["token"]}
    )


def _capture_from_fixture(**overrides) -> RequestRecipe:
    fx = _fixture()
    kwargs = dict(
        method=fx["method"],
        url=_captured_url(fx),
        headers=fx["headers"],
        request_body=None,
        source_page_url=fx["source_page_url"],
        window_start=datetime.fromisoformat(fx["window_start"].replace("Z", "+00:00")),
        window_end=datetime.fromisoformat(fx["window_end"].replace("Z", "+00:00")),
        horizon_days=fx["horizon_days"],
    )
    kwargs.update(overrides)
    return capture_recipe(**kwargs)


# --- capture / normalization -------------------------------------------------


def test_capture_strips_endpoint_and_keeps_required_params():
    recipe = _capture_from_fixture()
    assert recipe.endpoint.endswith("/plugins_events_events_by_date/find/")
    assert "?" not in recipe.endpoint  # query stripped from the endpoint itself
    assert set(recipe.query_params) == {"json", "token"}
    assert recipe.query_params["token"].kind == "literal"  # public token preserved


def test_capture_normalizes_date_range_to_placeholders():
    recipe = _capture_from_fixture()
    template = recipe.query_params["json"].value
    date_range = template["filter"]["date_range"]
    # MongoDB extended-JSON wrapper is preserved; only the inner value becomes
    # a placeholder.
    assert date_range["start"] == {"$date": {"kind": "window_start_utc"}}
    assert date_range["end"] == {"$date": {"kind": "window_end_utc"}}


def test_capture_normalizes_pagination_cursors():
    recipe = _capture_from_fixture()
    options = recipe.query_params["json"].value["options"]
    assert options["limit"] == {"kind": "page_limit"}
    assert options["skip"] == {"kind": "page_offset"}
    assert recipe.pagination.kind == "offset"
    assert recipe.pagination.limit == 100  # captured from the observed limit


def test_capture_preserves_boolean_count_not_as_limit():
    # `count: true` is a boolean flag, not a pagination size — it must survive.
    recipe = _capture_from_fixture()
    assert recipe.query_params["json"].value["options"]["count"] is True


def test_capture_leaves_unrelated_dates_literal():
    fx = _fixture()
    fx["query_json"]["filter"]["created_before_snapshot"] = "1999-01-01T00:00:00.000Z"
    recipe = capture_recipe(
        method=fx["method"], url=_captured_url(fx), headers=fx["headers"],
        request_body=None, source_page_url=fx["source_page_url"],
        window_start=None, window_end=None, horizon_days=7,
    )
    template = recipe.query_params["json"].value
    # A date NOT under a range-boundary key stays literal even with no window.
    assert template["filter"]["created_before_snapshot"] == "1999-01-01T00:00:00.000Z"
    # ...while the range boundaries still normalize by key name.
    assert template["filter"]["date_range"]["start"] == {"$date": {"kind": "window_start_utc"}}


def test_capture_discards_cookies_and_auth_headers():
    recipe = _capture_from_fixture()
    header_names = {h.lower() for h in recipe.headers}
    assert "cookie" not in header_names
    assert "authorization" not in header_names
    assert "sec-ch-ua" not in header_names
    assert "host" not in header_names
    # Only the safe allowlisted headers survive.
    assert header_names <= {
        "accept", "referer", "accept-language", "content-type", "x-requested-with",
    }


def test_capture_referer_becomes_dynamic_source_page_url():
    recipe = _capture_from_fixture()
    referer = next(v for k, v in recipe.headers.items() if k.lower() == "referer")
    assert referer.kind == "source_page_url"
    assert recipe.source_page_url == _fixture()["source_page_url"]


def test_capture_post_body_templated():
    fx = _fixture()
    recipe = capture_recipe(
        method="POST", url=fx["endpoint"],
        headers={"Content-Type": "application/json", "Referer": fx["source_page_url"]},
        request_body=json.dumps(fx["query_json"]), source_page_url=fx["source_page_url"],
        window_start=None, window_end=None, horizon_days=7,
    )
    assert recipe.method == "POST"
    assert recipe.body is not None and recipe.body.kind == "json_template"
    assert recipe.body.value["filter"]["date_range"]["start"] == {
        "$date": {"kind": "window_start_utc"}
    }


# --- rendering & dynamic date windows ----------------------------------------


def _render(recipe: RequestRecipe, now: datetime, tz: str | None):
    return render_recipe(recipe, now_utc=now, timezone=tz, page_offset=0, page_number=1)


def test_render_never_persists_absolute_dates_but_produces_them():
    recipe = _capture_from_fixture()
    # No absolute date survives in the stored recipe...
    dumped = recipe.model_dump_json()
    assert "2026-08-06T04:00:00" not in dumped
    # ...but rendering produces concrete UTC millisecond timestamps.
    rendered = _render(recipe, datetime(2026, 6, 15, 12, 0, tzinfo=UTC), "America/New_York")
    dr = json.loads(rendered.params["json"])["filter"]["date_range"]
    assert dr["start"]["$date"].endswith("Z")
    assert dr["start"]["$date"].count(".") == 1  # millisecond precision


def test_render_dst_summer_vs_winter_offset():
    recipe = _capture_from_fixture()
    tz = "America/New_York"
    summer = _render(recipe, datetime(2026, 7, 1, 12, 0, tzinfo=UTC), tz)
    winter = _render(recipe, datetime(2026, 1, 15, 12, 0, tzinfo=UTC), tz)
    summer_start = json.loads(summer.params["json"])["filter"]["date_range"]["start"]["$date"]
    winter_start = json.loads(winter.params["json"])["filter"]["date_range"]["start"]["$date"]
    # Start-of-day in EDT (UTC-4) vs EST (UTC-5) → different UTC wall clocks.
    assert summer_start.endswith("04:00:00.000Z")
    assert winter_start.endswith("05:00:00.000Z")


def test_render_horizon_sets_end_window():
    recipe = _capture_from_fixture()  # horizon_days=7
    rendered = _render(recipe, datetime(2026, 6, 1, 12, 0, tzinfo=UTC), "UTC")
    dr = json.loads(rendered.params["json"])["filter"]["date_range"]
    start = datetime.fromisoformat(dr["start"]["$date"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(dr["end"]["$date"].replace("Z", "+00:00"))
    # horizon_days=7 → an inclusive 7-calendar-day window (last day's final ms).
    assert start.date().isoformat() == "2026-06-01"
    assert end.date().isoformat() == "2026-06-07"
    assert end.strftime("%H:%M:%S.%f") == "23:59:59.999000"


def test_render_referer_and_pagination_offset():
    recipe = _capture_from_fixture()
    rendered = render_recipe(
        recipe, now_utc=datetime(2026, 6, 1, tzinfo=UTC), timezone="UTC",
        page_offset=200, page_number=3,
    )
    assert rendered.headers["Referer"] == _fixture()["source_page_url"]
    assert json.loads(rendered.params["json"])["options"]["skip"] == 200
    assert json.loads(rendered.params["json"])["options"]["limit"] == 100


# --- redaction / summary -----------------------------------------------------


def test_redact_token_is_non_reversible():
    hint = redact_token("PUBLIC-SITE-TOKEN-PLACEHOLDER-0123456789")
    assert "PLACEHOLDER" not in hint
    assert hint.startswith("PUB")
    assert "40 chars" in hint


def test_summary_redacts_token_and_lists_names_only():
    summary = summarize_recipe(_capture_from_fixture())
    assert summary["query_param_names"] == ["json", "token"]
    assert summary["public_token_present"] is True
    assert "PLACEHOLDER" not in (summary["public_token_hint"] or "")
    assert summary["referer_present"] is True
    assert summary["dynamic_date_window"] is True
    assert summary["pagination_kind"] == "offset"


# --- schema validation -------------------------------------------------------


def test_recipe_rejects_forbidden_header():
    with pytest.raises(pydantic.ValidationError):
        RequestRecipe(
            endpoint="https://events.example.org/api/find/",
            headers={"Cookie": RecipeValue(kind="literal", value="x")},
        )


def test_recipe_rejects_env_var_reference_in_template():
    with pytest.raises(pydantic.ValidationError):
        RecipeValue(kind="json_template", value={"token": "${SECRET_TOKEN}"})


def test_recipe_window_and_pagination_bounds():
    with pytest.raises(pydantic.ValidationError):
        RecipeWindow(horizon_days=0)
    with pytest.raises(pydantic.ValidationError):
        RecipePagination(kind="offset", limit=0)
    with pytest.raises(pydantic.ValidationError):
        RecipePagination(kind="offset", max_pages=999)


# --- end-to-end execution (the 403 -> 200 fix) -------------------------------


def _site_config_with_recipe():
    from app.schemas.extraction import SiteConfiguration

    recipe = _capture_from_fixture()
    return SiteConfiguration(
        pattern_name="simpleview_events",
        api_endpoint=recipe.endpoint,
        timezone="America/Indiana/Indianapolis",
        json_paths={"events_root": "docs.docs"},
        pagination={"strategy": "none", "max_pages": 5, "max_events": 500},
        max_detail_fetches=0,
        request_recipe=recipe,
    )


def _run_pipeline(config, handler):
    from app.extraction.fetch import HttpFetchStrategy
    from app.services.extraction_runs import _execute_pipeline

    async def _go():
        fetch = HttpFetchStrategy(transport=httpx.MockTransport(handler))
        return await _execute_pipeline(
            config, config.pattern_name, fetch, fallback_timezone=None
        )

    with patched_http_fetch(handler):  # SSRF DNS stub
        return asyncio.run(_go())


def test_recipe_execution_sends_json_token_and_referer_and_recovers_403():
    page1 = (FIXTURES_DIR / "simpleview_events_page1.json").read_text(encoding="utf-8")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        seen["params"] = params
        seen["referer"] = request.headers.get("referer")
        # Reproduce the real failure: without the full request the endpoint 403s.
        if "json" not in params or "token" not in params or not request.headers.get("referer"):
            return httpx.Response(403, text="Access Denied")
        return httpx.Response(200, text=page1, headers={"content-type": "application/json"})

    outcome = _run_pipeline(_site_config_with_recipe(), handler)

    assert seen["params"].get("token") == _fixture()["token"]
    assert seen["referer"] == _fixture()["source_page_url"]
    decoded = json.loads(seen["params"]["json"])
    assert decoded["filter"]["date_range"]["start"]["$date"].endswith("Z")
    # The 403 is gone: real event candidates came back.
    assert len(outcome.outcomes) > 0
    assert outcome.last_response.status_code == 200


def test_recipe_execution_sends_single_encoded_json_no_top_level_cursors():
    page1 = (FIXTURES_DIR / "simpleview_events_page1.json").read_text(encoding="utf-8")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["param_names"] = sorted(request.url.params.keys())
        seen["json_raw"] = request.url.params.get("json")
        return httpx.Response(200, text=page1, headers={"content-type": "application/json"})

    _run_pipeline(_site_config_with_recipe(), handler)

    # Exactly the captured params -- no top-level limit/skip/offset were added.
    assert seen["param_names"] == ["json", "token"]
    for cursor in ("limit", "skip", "offset", "page"):
        assert cursor not in seen["param_names"]
    # The json parameter is a single JSON encoding: it parses once to an object,
    # and is NOT a double-encoded JSON string (which would parse to a str).
    parsed = json.loads(seen["json_raw"])
    assert isinstance(parsed, dict)
    # The pagination cursors live INSIDE options, not as top-level query params.
    assert parsed["options"]["skip"] == 0
    assert isinstance(parsed["options"]["limit"], int)


def test_recipe_execution_reports_edge_protection_block():
    # An edge/WAF "Access Denied" (as the live Simpleview endpoint returns) is
    # surfaced as an edge_protection block, not a bare http_403, so the operator
    # learns the source is browser-only and cannot be HTTP-imported.
    akamai_body = (
        "<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD><BODY><H1>Access Denied</H1>"
        "You don't have permission to access this server. "
        "Reference #18 https://errors.edgesuite.net/18</BODY></HTML>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, text=akamai_body,
            headers={"content-type": "text/html", "x-sv-edge": "true", "akamai-grn": "0.x"},
        )

    outcome = _run_pipeline(_site_config_with_recipe(), handler)
    assert outcome.last_response.blocked_reason == "edge_protection:http_403"
    assert len(outcome.outcomes) == 0
    from app.extraction.fetch import describe_block_reason

    assert "edge/bot management" in describe_block_reason(outcome.last_response.blocked_reason)


def test_recipe_does_not_mutate_saved_recipe_across_pages():
    # Rendering successive pages must not mutate the stored recipe in place: its
    # json template keeps its placeholder for options.skip after execution.
    config = _paged_config(limit=2, total_path="docs.count")
    all_records = [_record(i) for i in range(3)]

    def handler(request: httpx.Request) -> httpx.Response:
        decoded = json.loads(dict(request.url.params)["json"])
        skip = decoded["options"]["skip"]
        page = all_records[skip: skip + 2]
        body = json.dumps({"docs": {"count": 3, "docs": page}})
        return httpx.Response(200, text=body, headers={"content-type": "application/json"})

    _run_pipeline(config, handler)
    # The persisted recipe template still holds the placeholder, unmutated.
    assert config.request_recipe.query_params["json"].value["options"]["skip"] == {
        "kind": "page_offset"
    }


def _record(i: int) -> dict:
    return {
        "recid": f"sv-{i}", "title": f"Event {i}",
        "startDate": "2026-10-06", "url": f"https://events.example.org/e/{i}/",
    }


def _paged_config(limit: int, total_path: str | None):
    from app.schemas.extraction import SiteConfiguration

    recipe = RequestRecipe(
        method="GET",
        endpoint="https://events.example.org/includes/rest_v2/find/",
        query_params={
            "json": RecipeValue(
                kind="json_template",
                value={"options": {"limit": {"kind": "page_limit"},
                                   "skip": {"kind": "page_offset"}}},
            )
        },
        pagination=RecipePagination(
            kind="offset", limit=limit, total_path=total_path, max_pages=10
        ),
    )
    return SiteConfiguration(
        pattern_name="simpleview_events",
        api_endpoint=recipe.endpoint,
        json_paths={"events_root": "docs.docs"},
        pagination={"strategy": "none", "max_pages": 10, "max_events": 500},
        max_detail_fetches=0,
        request_recipe=recipe,
    )


def test_recipe_offset_pagination_walks_pages_and_stops_on_short_page():
    # 5 records total, page size 2 → pages at skip 0, 2, 4 (last one short).
    all_records = [_record(i) for i in range(5)]
    skips_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        decoded = json.loads(dict(request.url.params)["json"])
        skip = decoded["options"]["skip"]
        limit = decoded["options"]["limit"]
        skips_seen.append(skip)
        page = all_records[skip: skip + limit]
        body = json.dumps({"docs": {"count": len(all_records), "docs": page}})
        return httpx.Response(200, text=body, headers={"content-type": "application/json"})

    outcome = _run_pipeline(_paged_config(limit=2, total_path="docs.count"), handler)
    assert skips_seen == [0, 2, 4]  # walked every page, stopped on the short one
    assert len(outcome.outcomes) == 5  # all records collected, deduped by recid hash


def test_recipe_offset_pagination_stops_on_total_reached():
    # Exactly 4 records, page size 2, total=4 → stop after skip 2 (total reached),
    # never fetching skip 4.
    all_records = [_record(i) for i in range(4)]
    skips_seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        decoded = json.loads(dict(request.url.params)["json"])
        skip = decoded["options"]["skip"]
        skips_seen.append(skip)
        page = all_records[skip: skip + 2]
        body = json.dumps({"docs": {"count": 4, "docs": page}})
        return httpx.Response(200, text=body, headers={"content-type": "application/json"})

    outcome = _run_pipeline(_paged_config(limit=2, total_path="docs.count"), handler)
    assert skips_seen == [0, 2]  # stopped once (page+1)*limit >= total
    assert len(outcome.outcomes) == 4


def test_recipe_execution_still_403_without_recipe():
    # Control: the bare endpoint (no recipe) reproduces the 403 the recipe fixes.
    from app.schemas.extraction import SiteConfiguration

    config = SiteConfiguration(
        pattern_name="simpleview_events",
        api_endpoint=_capture_from_fixture().endpoint,
        timezone="America/Indiana/Indianapolis",
        json_paths={"events_root": "docs.docs"},
        max_detail_fetches=0,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if "json" not in params or "token" not in params:
            return httpx.Response(403, text="Access Denied")
        return httpx.Response(200, text="{}", headers={"content-type": "application/json"})

    outcome = _run_pipeline(config, handler)
    assert outcome.last_response.status_code == 403
    assert outcome.last_response.blocked_reason is not None
    assert len(outcome.outcomes) == 0
