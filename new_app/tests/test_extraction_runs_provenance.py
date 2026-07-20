import pytest

from app.models.event import Event
from app.models.event_provenance import EventProvenance
from app.models.extraction_run import ExtractionRun
from app.models.unsupported_site_report import UnsupportedSiteReport
from app.schemas.extraction import SiteConfiguration
from app.services.extraction_runs import preview_extraction, run_detection, run_extraction
from app.services.website_configuration import approve_configuration
from tests.extraction_helpers import blocked_handler as _blocked_handler
from tests.extraction_helpers import html_handler as _html_handler
from tests.extraction_helpers import patched_http_fetch

JSONLD_CONFIG = SiteConfiguration(
    pattern_name="json_ld_event", listing_url="https://example.com/events"
)


@pytest.fixture
def website_with_listing(make_city, make_website):
    city = make_city()
    return make_website(city, name="Test Source", base_url="https://example.com")


async def _approve_with_current_preview(db_session, website, *, approved_by_user_id: int):
    """approve_configuration requires a successful preview at the current
    configuration_version — run one against the same fixture before
    approving, matching how a real admin would use the workflow."""
    with patched_http_fetch(_html_handler("jsonld_single_event.html")):
        await preview_extraction(db_session, website)
    return approve_configuration(db_session, website, approved_by_user_id=approved_by_user_id)


# --- Detection --------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_detection_never_persists_events_and_transitions_state(
    db_session, website_with_listing
):
    website = website_with_listing
    website.event_listing_url = "https://example.com/events"
    db_session.commit()

    with patched_http_fetch(_html_handler("jsonld_single_event.html")):
        result = await run_detection(db_session, website)

    assert result.status == "success"
    assert result.pattern == "json_ld_event"
    assert db_session.query(Event).count() == 0
    db_session.refresh(website)
    assert website.onboarding_status == "detected"
    assert website.proposed_pattern["detection"]["pattern_name"] == "json_ld_event"
    assert website.approved_pattern is None
    run = db_session.query(ExtractionRun).filter_by(run_type="detection").one()
    assert run.status == "success"


@pytest.mark.asyncio
async def test_unsupported_detection_creates_deduplicated_report(db_session, website_with_listing):
    website = website_with_listing
    website.event_listing_url = "https://example.com/events"
    db_session.commit()

    with patched_http_fetch(_html_handler("unsupported_page.html")):
        await run_detection(db_session, website)
        db_session.refresh(website)
        assert website.onboarding_status == "unsupported"
        await run_detection(db_session, website)  # identical page again

    reports = db_session.query(UnsupportedSiteReport).filter_by(website_id=website.id).all()
    assert len(reports) == 1  # unchanged report not duplicated


# --- Preview ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_never_persists_events(db_session, website_with_listing):
    website = website_with_listing
    website.configuration = JSONLD_CONFIG.model_dump(mode="json")
    db_session.commit()

    with patched_http_fetch(_html_handler("jsonld_single_event.html")):
        result = await preview_extraction(db_session, website)

    assert result.status == "success"
    assert result.events_valid == 1
    assert result.events_inserted == 0
    assert result.events_updated == 0
    assert db_session.query(Event).count() == 0
    assert db_session.query(EventProvenance).count() == 0
    run = db_session.query(ExtractionRun).filter_by(run_type="preview").one()
    assert run.events_inserted == 0


# --- Approval + persistence --------------------------------------------------


@pytest.mark.asyncio
async def test_run_extraction_requires_approved_configuration(db_session, website_with_listing):
    from app.core.exceptions import AppError

    with pytest.raises(AppError):
        await run_extraction(db_session, website_with_listing, triggered_by_user_id=None)


@pytest.mark.asyncio
async def test_approve_then_run_extraction_persists_event_with_provenance(
    db_session, website_with_listing, make_user
):
    website = website_with_listing
    admin = make_user(email="approver@example.com")
    website.configuration = JSONLD_CONFIG.model_dump(mode="json")
    db_session.commit()
    await _approve_with_current_preview(db_session, website, approved_by_user_id=admin.id)
    assert website.approved_pattern is not None

    with patched_http_fetch(_html_handler("jsonld_single_event.html")):
        result = await run_extraction(db_session, website, triggered_by_user_id=admin.id)

    assert result.status == "success"
    assert result.events_inserted == 1
    event = db_session.query(Event).one()
    assert event.title == "Jazz Night at the Park"
    provenance = db_session.query(EventProvenance).filter_by(event_id=event.id).one()
    assert provenance.extraction_pattern == "json_ld_event"
    assert provenance.extraction_run_id == result.run_id
    assert "title" in provenance.field_source_paths


@pytest.mark.asyncio
async def test_second_run_updates_event_and_preserves_admin_override(
    db_session, website_with_listing, make_user, make_category
):
    website = website_with_listing
    admin = make_user(email="approver2@example.com")
    website.configuration = JSONLD_CONFIG.model_dump(mode="json")
    db_session.commit()
    await _approve_with_current_preview(db_session, website, approved_by_user_id=admin.id)

    with patched_http_fetch(_html_handler("jsonld_single_event.html")):
        await run_extraction(db_session, website, triggered_by_user_id=admin.id)

    event = db_session.query(Event).one()
    override_category = make_category(name="Curated", slug="curated")
    event.category_override_id = override_category.id
    event.review_status = "reviewed"
    db_session.commit()

    with patched_http_fetch(_html_handler("jsonld_single_event.html")):
        result = await run_extraction(db_session, website, triggered_by_user_id=admin.id)

    assert result.events_updated == 1
    assert result.events_inserted == 0
    db_session.refresh(event)
    assert event.category_override_id == override_category.id  # preserved
    assert event.review_status == "reviewed"  # preserved
    assert (
        db_session.query(EventProvenance).filter_by(event_id=event.id).count() == 2
    )  # append-only


@pytest.mark.asyncio
async def test_archived_event_not_silently_reactivated_by_rescrape(
    db_session, website_with_listing, make_user
):
    website = website_with_listing
    admin = make_user(email="approver3@example.com")
    website.configuration = JSONLD_CONFIG.model_dump(mode="json")
    db_session.commit()
    await _approve_with_current_preview(db_session, website, approved_by_user_id=admin.id)

    with patched_http_fetch(_html_handler("jsonld_single_event.html")):
        await run_extraction(db_session, website, triggered_by_user_id=admin.id)

    from datetime import UTC, datetime

    event = db_session.query(Event).one()
    event.is_active = False
    event.archived_at = datetime.now(UTC)
    db_session.commit()

    with patched_http_fetch(_html_handler("jsonld_single_event.html")):
        await run_extraction(db_session, website, triggered_by_user_id=admin.id)

    db_session.refresh(event)
    assert event.is_active is False
    assert event.archived_at is not None


@pytest.mark.asyncio
async def test_blocked_response_marks_run_blocked_and_no_events_persisted(
    db_session, website_with_listing, make_user
):
    website = website_with_listing
    admin = make_user(email="approver4@example.com")
    website.configuration = JSONLD_CONFIG.model_dump(mode="json")
    db_session.commit()
    await _approve_with_current_preview(db_session, website, approved_by_user_id=admin.id)

    with patched_http_fetch(_blocked_handler(403)):
        result = await run_extraction(db_session, website, triggered_by_user_id=admin.id)

    assert result.status == "blocked"
    assert result.events_inserted == 0
    assert db_session.query(Event).count() == 0
    db_session.refresh(website)
    assert website.consecutive_failure_count == 1
    assert website.last_failure_at is not None


@pytest.mark.asyncio
async def test_repeated_failures_auto_transition_active_website_to_failing(
    db_session, website_with_listing, make_user
):
    website = website_with_listing
    admin = make_user(email="approver5@example.com")
    website.configuration = JSONLD_CONFIG.model_dump(mode="json")
    db_session.commit()
    await _approve_with_current_preview(db_session, website, approved_by_user_id=admin.id)
    website.onboarding_status = "active"
    website.is_active = True
    db_session.commit()

    for _ in range(3):
        with patched_http_fetch(_blocked_handler(403)):
            await run_extraction(db_session, website, triggered_by_user_id=admin.id)

    db_session.refresh(website)
    assert website.onboarding_status == "failing"
