"""End-to-end automatic onboarding: detect -> configure -> preview -> score.

These tests drive the real services with a mocked HTTP transport (see
`patched_http_fetch`), so the whole production path runs — SSRF validation,
fetch, detection, inference, draft save, preview, quality scoring — with zero
live network calls.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.extraction.inference.policy import NEEDS_REVIEW, READY_FOR_APPROVAL
from app.models.event import Event
from app.services.onboarding_automation import detect_and_configure
from tests.extraction_helpers import load_fixture, patched_http_fetch

LISTING_URL = "https://hall.example.org/events"
YEARLESS_LISTING_URL = "https://venue.example.org/events/"
MIXED_LISTING_URL = "https://community.example.org/events"


def _routing_handler(routes: dict[str, str], default_status: int = 404):
    """Serves a fixture per URL path; anything unmapped 404s, which is how the
    detail-probe-failure path is exercised."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = routes.get(str(request.url))
        if body is None:
            return httpx.Response(default_status, text="not found")
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    return handler


def _run(coro):
    return asyncio.run(coro)


def _configure(db_session, website, routes):
    with patched_http_fetch(_routing_handler(routes)):
        return _run(detect_and_configure(db_session, website))


# --- ready for approval ------------------------------------------------------


@pytest.fixture
def ready_result(db_session, make_city, make_website):
    city = make_city(name="Auto City", slug="auto-city", timezone="America/Indiana/Indianapolis")
    website = make_website(city, name="Auto Site", base_url=LISTING_URL)
    result = _configure(
        db_session,
        website,
        {LISTING_URL: load_fixture("inference_cards_weekday_date.html")},
    )
    db_session.refresh(website)
    return result, website


def test_detect_and_configure_reaches_ready_for_approval(ready_result):
    result, _ = ready_result
    assert result.outcome == READY_FOR_APPROVAL
    assert result.blocking_reasons == ()
    assert result.inference.pattern_name == "generic_html_cards"


def test_a_complete_configuration_is_saved_as_a_draft(ready_result):
    _, website = ready_result
    config = website.configuration
    assert config is not None
    assert config["event_container_selector"] == "div.eventslist__item"
    assert config["date_formats"] == ["%A | %b %d , %Y"]
    for field_name in ("title", "canonical_url", "start_datetime"):
        assert field_name in config["field_selectors"]
    # The draft is versioned like any other configuration save.
    assert website.configuration_version >= 1


def test_preview_runs_automatically_and_is_scored(ready_result):
    result, _ = ready_result
    assert result.preview is not None
    assert result.preview.status in ("success", "partial")
    quality = result.quality
    assert quality is not None
    assert quality.candidates_found == 5
    assert quality.valid_count == 5
    assert quality.valid_percentage == 1.0
    assert quality.date_parse_success_rate == 1.0
    assert quality.url_validity_rate == 1.0
    assert quality.required_field_coverage["title"] == 1.0


def test_automatic_preview_persists_no_events(ready_result, db_session):
    _, website = ready_result
    assert db_session.query(Event).filter(Event.website_id == website.id).count() == 0
    assert db_session.query(Event).count() == 0


def test_result_is_recorded_on_the_website_for_review(ready_result):
    result, website = ready_result
    stored = website.proposed_pattern["inference"]
    assert stored["outcome"] == result.outcome
    assert stored["quality"]["valid_count"] == 5
    assert stored["samples"]["valid"][0]["title"] == "Autumn Quartet"
    assert any(c["field"] == "title" for c in stored["inference"]["field_candidates"])


def test_approval_is_never_automatic(ready_result):
    _, website = ready_result
    assert website.approved_pattern is None
    assert website.is_active is False


# --- detail-page enrichment --------------------------------------------------


def test_yearless_cards_are_resolved_through_a_bounded_detail_probe(
    db_session, make_city, make_website
):
    city = make_city(name="Probe City", slug="probe-city", timezone="UTC")
    website = make_website(city, name="Probe Site", base_url=YEARLESS_LISTING_URL)
    result = _configure(
        db_session,
        website,
        {
            YEARLESS_LISTING_URL: load_fixture("inference_cards_yearless_date.html"),
            "https://venue.example.org/events/quest-film": load_fixture(
                "inference_detail_full_date.html"
            ),
        },
    )
    db_session.refresh(website)

    assert "detail_start_datetime" in website.configuration["field_selectors"]
    assert result.quality is not None
    assert result.quality.detail_fetch_used is True
    # The one card whose detail page is served resolves a real date; the rest
    # 404 and are rejected rather than guessed at.
    assert result.quality.valid_count >= 1
    assert db_session.query(Event).count() == 0


def test_a_failed_detail_probe_does_not_fabricate_a_date(db_session, make_city, make_website):
    city = make_city(name="No Probe City", slug="no-probe-city", timezone="UTC")
    website = make_website(city, name="No Probe Site", base_url=YEARLESS_LISTING_URL)
    result = _configure(
        db_session,
        website,
        {YEARLESS_LISTING_URL: load_fixture("inference_cards_yearless_date.html")},
    )
    assert result.outcome == NEEDS_REVIEW
    assert result.inference.configuration is None
    assert db_session.query(Event).count() == 0


# --- needs review ------------------------------------------------------------


def test_low_valid_percentage_is_classified_as_needs_review(
    db_session, make_city, make_website
):
    city = make_city(name="Mixed City", slug="mixed-city", timezone="UTC")
    website = make_website(city, name="Mixed Site", base_url=MIXED_LISTING_URL)
    result = _configure(
        db_session,
        website,
        {MIXED_LISTING_URL: load_fixture("inference_cards_mixed_quality.html")},
    )
    db_session.refresh(website)

    assert result.outcome == NEEDS_REVIEW
    assert result.quality.valid_percentage < 1.0
    assert any("valid percentage" in reason for reason in result.blocking_reasons)
    assert website.onboarding_status == "needs_review"
    assert db_session.query(Event).count() == 0


# --- routes ------------------------------------------------------------------


def _csrf(client) -> str:
    return client.cookies.get("csrf_token")


def test_detect_and_configure_route_and_review_screen(
    client, make_super_admin, make_city, make_website, login, db_session
):
    make_super_admin(email="auto-root@example.com", password="root-pass-1234")
    city = make_city(name="Route City", slug="route-city", timezone="UTC")
    website = make_website(city, name="Route Site", base_url=LISTING_URL)
    login("auto-root@example.com", "root-pass-1234")

    with patched_http_fetch(
        _routing_handler({LISTING_URL: load_fixture("inference_cards_weekday_date.html")})
    ):
        resp = client.post(
            f"/admin/websites/{website.id}/detect-and-configure",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/admin/websites/{website.id}/onboarding"

    page = client.get(f"/admin/websites/{website.id}/onboarding")
    assert page.status_code == 200
    body = page.text
    assert "ready for approval" in body
    assert "generic_html_cards" in body
    assert "div.eventslist__item" in body
    assert "Autumn Quartet" in body
    assert "Approve" in body
    assert "Edit advanced configuration" in body
    # The main path never asks an administrator to write JSON.
    assert "<textarea" not in body
    assert db_session.query(Event).count() == 0


def test_detect_and_configure_requires_the_test_permission(
    client, make_user, make_city, make_website, login
):
    from app.core.permissions import REGISTERED_USER

    make_user(
        email="auto-plain@example.com", password="user-pass-1234", role_name=REGISTERED_USER
    )
    city = make_city(name="Perm City", slug="perm-city", timezone="UTC")
    website = make_website(city, name="Perm Site", base_url=LISTING_URL)
    login("auto-plain@example.com", "user-pass-1234")

    resp = client.post(
        f"/admin/websites/{website.id}/detect-and-configure",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303, 403)
    if resp.status_code in (302, 303):
        assert "/admin/websites" not in resp.headers["location"]
