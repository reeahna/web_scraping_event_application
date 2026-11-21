"""The admin pages show the latest preview's own quality.

Before, the onboarding page and the website page always showed the quality
snapshot taken by automatic onboarding, so a later preview (run after a fix)
left the old numbers on screen (Bloomington Online: "valid 72%" next to a
40 / 40 / 0 preview).
"""

import copy
from datetime import UTC, datetime

import pytest

from app.core.onboarding_jobs import READY_FOR_APPROVAL
from app.models.extraction_run import ExtractionRun
from app.schemas.extraction import SiteConfiguration
from app.services.extraction_runs import preview_extraction
from tests.extraction_helpers import html_handler, patched_http_fetch

_SNAPSHOT = {
    "candidates_found": 40, "valid_count": 29, "rejected_count": 11,
    "valid_percentage": 0.725, "rejected_percentage": 0.275,
    "required_field_coverage": {"title": 1.0, "start_date": 1.0, "canonical_url": 0.725},
    "date_parse_success_rate": 1.0, "url_validity_rate": 0.725, "duplicate_rate": 0.07,
    "warning_count": 0, "pagination_truncated": False, "detail_fetch_used": False,
    "pages_fetched": 1,
}
_LATEST = {
    **_SNAPSHOT,
    "valid_count": 40, "rejected_count": 0, "valid_percentage": 1.0,
    "rejected_percentage": 0.0, "url_validity_rate": 1.0,
    "required_field_coverage": {"title": 1.0, "start_date": 1.0, "canonical_url": 1.0},
}


@pytest.fixture
def admin_client(client, make_super_admin, login):
    make_super_admin(email="run-quality@example.com", password="root-pass-1234")
    login("run-quality@example.com", "root-pass-1234")
    return client


def _website(db_session, make_city, make_website):
    website = make_website(make_city(), approved_pattern=None)
    website.onboarding_status = READY_FOR_APPROVAL
    website.proposed_pattern = {
        "inference": {
            "outcome": "ready_for_approval",
            "generated_at": "2026-11-20T02:00:00+00:00",
            "inference": {
                "pattern_name": "json_ld_event",
                "detection_confidence": 0.95, "proposal_confidence": 0.9,
                "field_candidates": [], "date_format_candidates": [],
                "missing_required_fields": [], "warnings": [], "error": None,
                "configuration": None,
            },
            "quality": copy.deepcopy(_SNAPSHOT),
            "samples": None,
            "blocking_reasons": [],
        }
    }
    db_session.commit()
    return website


def _preview_run(db_session, website, quality):
    run = ExtractionRun(
        website_id=website.id,
        configuration_version=website.configuration_version,
        pattern_name="json_ld_event",
        run_type="preview",
        status="success",
        source_url="https://example.com/events",
        events_found=40,
        events_valid=40 if quality is _LATEST else 29,
        events_rejected=0 if quality is _LATEST else 11,
        quality=copy.deepcopy(quality) if quality else None,
        started_at=datetime(2026, 11, 20, 21, 6, tzinfo=UTC),
        completed_at=datetime(2026, 11, 20, 21, 6, tzinfo=UTC),
    )
    db_session.add(run)
    db_session.commit()
    return run


def test_onboarding_page_shows_the_latest_preview_quality(
    admin_client, make_city, make_website, db_session
):
    website = _website(db_session, make_city, make_website)
    run = _preview_run(db_session, website, _LATEST)
    body = admin_client.get(f"/admin/websites/{website.id}/onboarding").text
    assert f"run #{run.id}</a>" in body
    assert "40 (100%)" in body
    assert "29 (72%)" not in body and "29 (73%)" not in body


def test_onboarding_page_falls_back_to_the_onboarding_snapshot(
    admin_client, make_city, make_website, db_session
):
    website = _website(db_session, make_city, make_website)
    _preview_run(db_session, website, None)  # recorded before quality was kept
    body = admin_client.get(f"/admin/websites/{website.id}/onboarding").text
    assert "From automatic onboarding." in body
    assert "Valid</th><td>29 (" in body


def test_website_page_shows_the_latest_preview_quality(
    admin_client, make_city, make_website, db_session
):
    website = _website(db_session, make_city, make_website)
    _preview_run(db_session, website, _LATEST)
    body = admin_client.get(f"/admin/websites/{website.id}").text
    assert "valid 100%, date-parse 100%, URLs 100%" in body
    assert "from automatic onboarding" not in body


def test_website_page_labels_an_onboarding_snapshot(
    admin_client, make_city, make_website, db_session
):
    website = _website(db_session, make_city, make_website)
    _preview_run(db_session, website, None)
    body = admin_client.get(f"/admin/websites/{website.id}").text
    assert "(from automatic onboarding, not this run)" in body


@pytest.mark.asyncio
async def test_a_preview_run_records_its_quality(make_city, make_website, db_session):
    website = make_website(make_city())
    website.configuration = SiteConfiguration(
        pattern_name="json_ld_event", listing_url="https://example.com/events"
    ).model_dump(mode="json")
    db_session.commit()

    with patched_http_fetch(html_handler("jsonld_multiple_events.html")):
        result = await preview_extraction(db_session, website)

    run = db_session.get(ExtractionRun, result.run_id)
    assert run.quality is not None
    assert run.quality["candidates_found"] == result.events_found
    assert run.quality["valid_count"] == result.events_valid
