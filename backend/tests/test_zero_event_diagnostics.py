"""A run that reads zero events records what the source actually sent, so a
blocked, empty or different page can be told apart from an empty calendar.

The record is structure only: no page text except the title, no cookies,
credentials or other sensitive headers, no query-string values, no JSON values.
Recording it changes nothing else about the run or the source.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from app.extraction import diagnostics
from app.extraction.diagnostics import describe_zero_event_response, sanitize_url
from app.models.event import Event
from app.models.extraction_run import ExtractionRun
from app.schemas.extraction import SiteConfiguration
from app.services.extraction_runs import preview_extraction, run_extraction
from app.services.website_configuration import approve_configuration
from tests.extraction_helpers import make_response, patched_http_fetch

CARDS = SiteConfiguration(
    pattern_name="generic_html_cards",
    listing_url="https://example.com/events",
    event_container_selector="div.eventItem",
    field_selectors={
        "title": {"kind": "css", "selector": "h3.title"},
        "canonical_url": {"kind": "attribute", "selector": "a.more", "attribute": "href"},
        "start_datetime": {"kind": "css", "selector": "time"},
    },
)

SECRET = "s3cr3t-value-never-stored"

# A page that is not the listing: a long article, a form, hidden values,
# a tokenised link and an inline script, none of which may be recorded.
OTHER_PAGE = f"""<!doctype html><html><head><title>  Welcome   to the Arts Center </title>
<script>var apiKey = "{SECRET}";</script></head>
<body><header id="top" class="site-header"><nav class="menu"><a href="/a?token={SECRET}">A</a>
<a href="/b">B</a></nav></header>
<main class="content"><p>{"Private paragraph text that should not be stored. " * 20}</p>
<form class="signup"><input type="hidden" name="csrf" value="{SECRET}"></form>
<div data-token="{SECRET}" class="card">x</div><div class="card">y</div>
<div class="card">z</div></main></body></html>"""

LISTING = """<html><head><title>Events</title></head><body><main>
<div class="eventItem"><span>Jazz</span></div><div class="eventItem"><span>Folk</span></div>
</main></body></html>"""


def _describe(body, config=CARDS, **kwargs):
    return describe_zero_event_response(make_response(body, **kwargs), config)


# --- what is recorded ------------------------------------------------------------


def test_urls_lose_user_info_query_values_and_fragment():
    url = f"https://user:{SECRET}@example.com:8443/events?key={SECRET}&page=2#frag"
    assert sanitize_url(url) == "https://example.com:8443/events?key=...&page=..."
    assert sanitize_url(None) is None


def test_only_allowlisted_headers_are_kept():
    summary = _describe(
        LISTING,
        headers={
            "Set-Cookie": f"session={SECRET}",
            "Cookie": f"a={SECRET}",
            "Authorization": f"Bearer {SECRET}",
            "X-Api-Key": SECRET,
            "Server": "nginx",
            "CF-Cache-Status": "HIT",
            "X-Device-Type": "default",
        },
    )
    assert summary["headers"] == {
        "content-type": "text/html",
        "server": "nginx",
        "cf-cache-status": "HIT",
        "x-device-type": "default",
    }
    assert SECRET not in json.dumps(summary)


def test_status_url_redirects_type_and_size():
    response = make_response(
        LISTING,
        final_url=f"https://example.com/home?sid={SECRET}",
        redirect_history=(f"https://example.com/events?sid={SECRET}",),
    )
    response = type(response)(**{**response.__dict__, "request_url": "https://example.com/events"})
    summary = describe_zero_event_response(response, CARDS)
    assert summary["status_code"] == 200
    assert summary["request_url"] == "https://example.com/events"
    assert summary["final_url"] == "https://example.com/home?sid=..."
    assert summary["redirected"] is True
    assert summary["redirects"] == ["https://example.com/events?sid=..."]
    assert summary["content_type"] == "text/html"
    assert summary["size_bytes"] == len(LISTING.encode())
    assert SECRET not in json.dumps(summary)


def test_page_structure_without_page_text_or_attribute_values():
    summary = _describe(OTHER_PAGE)
    html = summary["html"]
    stored = json.dumps(summary)

    assert html["title"] == "Welcome to the Arts Center"
    assert SECRET not in stored
    assert "Private paragraph" not in stored
    assert html["selector_matches"] == {
        "event_container_selector": 0, "field:title": 0, "field:canonical_url": 0,
        "field:start_datetime": 0,
    }
    assert html["counts"]["form"] == 1 and html["counts"]["script"] == 1
    assert "card x3" in html["common_classes"]
    assert "body" in html["skeleton"][0]
    assert any("header#top.site-header" in line for line in html["skeleton"])
    # Three identical cards are outlined once, then summarised.
    assert sum("div.card" in line and "more" not in line for line in html["skeleton"]) == 1
    assert any("... more div.card" in line for line in html["skeleton"])


def test_skeleton_is_bounded():
    page = "<html><body>" + "".join(
        f'<section class="s{i}"><div><div><div><div><p>x</p></div></div></div></div></section>'
        for i in range(200)
    ) + "</body></html>"
    skeleton = _describe(page)["html"]["skeleton"]
    assert len(skeleton) <= diagnostics.MAX_SKELETON_LINES
    assert all(line.count("  ") <= diagnostics.MAX_SKELETON_DEPTH for line in skeleton)


def test_json_records_keys_and_types_never_values():
    body = json.dumps({"events": [{"title": SECRET, "id": 7}], "token": SECRET, "total": 1})
    summary = _describe(body, content_type="application/json")
    assert summary["json"] == {
        "type": "object",
        "key_count": 3,
        "keys": {"events": "list", "token": "str", "total": "int"},
    }
    assert SECRET not in json.dumps(summary)


def test_calendar_feed_counts_entries():
    summary = _describe(
        "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", content_type="text/calendar"
    )
    assert summary["ics"] == {"vevent_count": 0}
    assert summary["verdict"] == "empty"


# --- the verdict ---------------------------------------------------------------


@pytest.mark.parametrize(
    "body,kwargs,verdict",
    [
        (LISTING, {"status_code": 403}, "blocked"),
        (LISTING, {"status_code": 429}, "blocked"),
        (LISTING, {"blocked_reason": "http_403"}, "blocked"),
        ("<html><body><h1>Access denied</h1></body></html>", {}, "blocked"),
        (LISTING, {"status_code": 500}, "error_status"),
        ("", {}, "empty"),
        ("<html><head><title>x</title></head><body></body></html>", {}, "empty"),
        ("[]", {"content_type": "application/json"}, "empty"),
        (OTHER_PAGE, {}, "different_page"),
        (LISTING + "<p>" + "Lots of text. " * 30 + "</p>", {}, "listing_found_but_not_read"),
    ],
)
def test_verdict(body, kwargs, verdict):
    assert _describe(body, **kwargs)["verdict"] == verdict


def test_a_bad_selector_is_reported_not_raised():
    config = CARDS.model_copy(update={"event_container_selector": "div[[["})
    summary = _describe(OTHER_PAGE, config=config)
    assert summary["html"]["selector_matches"]["event_container_selector"] == "invalid selector"


def test_diagnostics_never_raise(monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(diagnostics, "_html_summary", broken)
    summary = _describe(OTHER_PAGE)
    assert summary == {"error": "diagnostics failed: RuntimeError"}


# --- through a run ---------------------------------------------------------------


def _handler(body: str, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, text=body,
            headers={"content-type": "text/html", "set-cookie": f"sid={SECRET}"},
        )

    return handler


@pytest.fixture
def cards_website(db_session, make_city, make_website):
    website = make_website(make_city(), name="Arts", base_url="https://example.com")
    website.configuration = CARDS.model_dump(mode="json")
    db_session.commit()
    return website


@pytest.mark.asyncio
async def test_preview_with_no_events_records_what_came_back(db_session, cards_website):
    before = (cards_website.configuration_version, cards_website.approved_pattern,
              cards_website.is_active, cards_website.schedule_config)
    with patched_http_fetch(_handler(OTHER_PAGE)):
        await preview_extraction(db_session, cards_website)

    run = db_session.query(ExtractionRun).one()
    found = run.response_metadata["zero_event_diagnostics"]
    assert found["verdict"] == "different_page"
    assert found["html"]["title"] == "Welcome to the Arts Center"
    assert SECRET not in json.dumps(run.response_metadata)
    # Read-only: nothing imported, nothing about the source changed.
    assert db_session.query(Event).count() == 0
    db_session.refresh(cards_website)
    assert (cards_website.configuration_version, cards_website.approved_pattern,
            cards_website.is_active, cards_website.schedule_config) == before


@pytest.mark.asyncio
async def test_import_with_no_events_records_and_logs_it(
    db_session, cards_website, make_user, caplog
):
    page = LISTING.replace("<span>Jazz</span>", '<h3 class="title">Jazz</h3>'
                           '<a class="more" href="/e/1">More</a><time>2030-06-01 19:00</time>')
    with patched_http_fetch(_handler(page)):
        await preview_extraction(db_session, cards_website)
    approve_configuration(
        db_session, cards_website, approved_by_user_id=make_user(email="a@example.com").id
    )

    with caplog.at_level(logging.INFO, logger="app.extraction.diagnostics"), \
            patched_http_fetch(_handler(OTHER_PAGE)):
        result = await run_extraction(db_session, cards_website, triggered_by_user_id=None)

    run = db_session.get(ExtractionRun, result.run_id)
    assert run.events_found == 0
    assert run.response_metadata["zero_event_diagnostics"]["verdict"] == "different_page"
    line = next(r.getMessage() for r in caplog.records if "zero events" in r.getMessage())
    assert f"website {cards_website.id} (import run)" in line
    assert "verdict=different_page" in line and "status=200" in line
    assert SECRET not in line


@pytest.mark.asyncio
async def test_runs_that_read_events_record_no_diagnostics(db_session, cards_website):
    page = LISTING.replace("<span>Jazz</span>", '<h3 class="title">Jazz</h3>'
                           '<a class="more" href="/e/1">More</a>')
    with patched_http_fetch(_handler(page)):
        await preview_extraction(db_session, cards_website)
    run = db_session.query(ExtractionRun).one()
    assert run.events_found > 0
    assert "zero_event_diagnostics" not in run.response_metadata


@pytest.mark.asyncio
async def test_run_page_shows_what_the_source_sent(
    db_session, cards_website, client, make_super_admin, login
):
    with patched_http_fetch(_handler(OTHER_PAGE)):
        await preview_extraction(db_session, cards_website)
    run = db_session.query(ExtractionRun).one()
    make_super_admin(email="runs@example.com", password="x")
    login("runs@example.com", "x")

    body = client.get(f"/admin/websites/{cards_website.id}/runs/{run.id}").text
    assert "What the source sent (no events read)" in body
    assert "different_page" in body
    assert "Page outline" in body
    assert SECRET not in body
