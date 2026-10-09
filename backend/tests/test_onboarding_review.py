import pytest

from app.core.exceptions import AppError
from app.core.onboarding import DRAFT, NEEDS_REVIEW, UNSUPPORTED, can_transition
from app.extraction.detection import RELIABILITY_ORDER
from app.models.audit_log import AuditLog
from app.models.website import Website
from app.services.extraction_runs import run_detection
from app.services.website_configuration import select_pattern
from tests.extraction_helpers import html_handler, patched_http_fetch


def _csrf(client) -> str:
    return client.cookies.get("csrf_token")


@pytest.fixture
def website_with_listing(make_city, make_website):
    city = make_city()
    website = make_website(city, name="Review Source", base_url="https://example.com")
    website.event_listing_url = "https://example.com/events"
    return website


# --- State machine ------------------------------------------------------------------


def test_needs_review_reachable_from_draft_detecting_and_unsupported():
    assert can_transition(DRAFT, NEEDS_REVIEW)
    assert can_transition("detecting", NEEDS_REVIEW)
    assert can_transition(UNSUPPORTED, NEEDS_REVIEW)


def test_needs_review_still_not_reachable_from_active():
    assert not can_transition("active", NEEDS_REVIEW)


# --- Low-confidence detection --------------------------------------------------------


@pytest.mark.asyncio
async def test_low_confidence_detection_moves_to_needs_review_not_unsupported(
    db_session, website_with_listing
):
    website = website_with_listing
    db_session.commit()

    with patched_http_fetch(html_handler("wordpress_low_confidence.html")):
        result = await run_detection(db_session, website)

    assert result.status == "needs_review"
    db_session.refresh(website)
    assert website.onboarding_status == "needs_review"
    # Never silently promoted: pattern_name stays None even though a
    # plausible-but-below-threshold candidate existed.
    assert website.proposed_pattern["detection"]["pattern_name"] is None
    assert website.proposed_pattern["detection"]["evidence"]["winner"] == "wordpress_rest"


@pytest.mark.asyncio
async def test_browser_required_page_flagged_and_reported(
    db_session, website_with_listing, make_user
):
    from app.core.permissions import ADMINISTRATOR
    from app.models.notification import Notification
    from app.models.unsupported_site_report import UnsupportedSiteReport

    website = website_with_listing
    admin = make_user(email="browser-required-admin@example.com", role_name=ADMINISTRATOR)
    db_session.commit()

    with patched_http_fetch(html_handler("browser_required_page.html")):
        result = await run_detection(db_session, website)

    assert result.status == "unsupported"
    db_session.refresh(website)
    assert website.proposed_pattern["detection"]["browser_required"] is True

    report = db_session.query(UnsupportedSiteReport).filter_by(website_id=website.id).one()
    assert report.browser_required is True

    notification = (
        db_session.query(Notification)
        .filter_by(notification_type="website_browser_required", recipient_user_id=admin.id)
        .first()
    )
    assert notification is not None


@pytest.mark.asyncio
async def test_genuinely_unsupported_detection_still_moves_to_unsupported(
    db_session, website_with_listing
):
    website = website_with_listing
    db_session.commit()

    with patched_http_fetch(html_handler("unsupported_page.html")):
        result = await run_detection(db_session, website)

    assert result.status == "unsupported"
    db_session.refresh(website)
    assert website.onboarding_status == "unsupported"


@pytest.mark.asyncio
async def test_all_detector_results_carry_their_own_confidence(db_session, website_with_listing):
    website = website_with_listing
    db_session.commit()

    with patched_http_fetch(html_handler("jsonld_single_event.html")):
        await run_detection(db_session, website)

    db_session.refresh(website)
    all_results = website.proposed_pattern["detection"]["evidence"]["all_results"]
    # Every registered detector reports, not just the winner — derived from
    # the reliability order so registering a new pattern doesn't silently
    # leave this assertion behind.
    assert set(all_results.keys()) == set(RELIABILITY_ORDER)
    for name, result in all_results.items():
        assert "confidence" in result, name
        assert "needs_review" in result, name
        assert "browser_required" in result, name


# --- Manual pattern selection ---------------------------------------------------------


def test_select_pattern_from_draft_moves_to_needs_review_and_scaffolds_configuration(
    db_session, website_with_listing
):
    website = website_with_listing
    db_session.commit()

    select_pattern(db_session, website, pattern_name="json_ld_event")

    assert website.onboarding_status == "needs_review"
    assert website.is_active is False
    assert website.configuration["pattern_name"] == "json_ld_event"
    assert website.configuration["listing_url"] == "https://example.com/events"
    assert website.configuration_version == 1


def test_select_pattern_from_unsupported_is_allowed(db_session, website_with_listing):
    from app.services.websites import transition_website

    website = website_with_listing
    db_session.commit()
    transition_website(db_session, website, "detecting")
    transition_website(db_session, website, "unsupported")

    select_pattern(db_session, website, pattern_name="generic_html_cards")
    assert website.onboarding_status == "needs_review"


def test_select_pattern_rejects_unregistered_pattern_name(db_session, website_with_listing):
    website = website_with_listing
    db_session.commit()

    with pytest.raises(AppError):
        select_pattern(db_session, website, pattern_name="totally_made_up_pattern")


def test_select_pattern_never_bypasses_preview_or_approval(db_session, website_with_listing):
    """Manual selection only ever produces a draft — approved_pattern must
    stay untouched until the normal preview/approve flow runs."""
    website = website_with_listing
    db_session.commit()

    select_pattern(db_session, website, pattern_name="json_ld_event")
    assert website.approved_pattern is None


def test_select_pattern_route_is_audited(
    client, make_super_admin, make_city, make_website, login, db_session
):
    make_super_admin(email="select-root@example.com", password="root-pass-1234")
    city = make_city(name="Select City", slug="select-city")
    website = make_website(city, name="Select Site")
    login("select-root@example.com", "root-pass-1234")

    client.get(f"/admin/websites/{website.id}")
    resp = client.post(
        f"/admin/websites/{website.id}/select-pattern",
        data={"pattern_name": "json_ld_event", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.refresh(website)
    assert website.onboarding_status == "needs_review"

    entries = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "website_pattern_manually_selected")
        .all()
    )
    assert len(entries) == 1
    assert entries[0].entity_id == website.id


def test_select_pattern_route_requires_sites_update_permission(
    client, make_user, make_city, make_website, login, db_session
):
    from app.core.permissions import REGISTERED_USER

    city = make_city(name="Select Denied City", slug="select-denied-city")
    website = make_website(city, name="Select Denied Site")
    make_user(
        email="select-denied@example.com", password="denied-pass-123", role_name=REGISTERED_USER
    )
    login("select-denied@example.com", "denied-pass-123")

    resp = client.post(
        f"/admin/websites/{website.id}/select-pattern",
        data={"pattern_name": "json_ld_event", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    assert db_session.get(Website, website.id).onboarding_status == "draft"
