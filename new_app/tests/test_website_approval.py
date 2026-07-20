import pytest

from app.core.exceptions import AppError
from app.models.audit_log import AuditLog
from app.schemas.extraction import SiteConfiguration
from app.services.extraction_runs import preview_extraction
from app.services.website_configuration import approve_configuration, reject_configuration
from tests.extraction_helpers import blocked_handler, html_handler, patched_http_fetch

JSONLD_CONFIG = SiteConfiguration(
    pattern_name="json_ld_event", listing_url="https://example.com/events"
)


@pytest.fixture
def draft_website(make_city, make_website, make_user):
    city = make_city()
    website = make_website(city, name="Approval Source", base_url="https://example.com")
    website.configuration = JSONLD_CONFIG.model_dump(mode="json")
    admin = make_user(email="approver-test@example.com")
    return website, admin


def _csrf(client) -> str:
    return client.cookies.get("csrf_token")


# --- Stale-preview protection --------------------------------------------------------


def test_approval_blocked_when_no_preview_exists(db_session, draft_website):
    website, admin = draft_website
    db_session.commit()

    with pytest.raises(AppError) as exc:
        approve_configuration(db_session, website, approved_by_user_id=admin.id)
    assert "preview" in str(exc.value).lower()
    assert website.approved_pattern is None


@pytest.mark.asyncio
async def test_approval_blocked_when_preview_is_stale_after_draft_edit(db_session, draft_website):
    from app.services.website_configuration import save_draft_configuration

    website, admin = draft_website
    db_session.commit()

    with patched_http_fetch(html_handler("jsonld_single_event.html")):
        await preview_extraction(db_session, website)

    # Editing the draft after preview bumps configuration_version.
    save_draft_configuration(
        db_session,
        website,
        SiteConfiguration(pattern_name="json_ld_event", listing_url="https://example.com/events2"),
    )

    with pytest.raises(AppError) as exc:
        approve_configuration(db_session, website, approved_by_user_id=admin.id)
    assert "changed after" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_approval_blocked_when_latest_preview_failed(db_session, draft_website):
    website, admin = draft_website
    db_session.commit()

    with patched_http_fetch(html_handler("unsupported_page.html")):
        await preview_extraction(db_session, website)

    with pytest.raises(AppError) as exc:
        approve_configuration(db_session, website, approved_by_user_id=admin.id)
    assert "did not complete successfully" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_approval_succeeds_with_current_successful_preview(db_session, draft_website):
    website, admin = draft_website
    db_session.commit()

    with patched_http_fetch(html_handler("jsonld_single_event.html")):
        await preview_extraction(db_session, website)

    approve_configuration(db_session, website, approved_by_user_id=admin.id)
    assert website.approved_pattern is not None
    assert website.approved_at is not None
    assert website.approved_by_user_id == admin.id
    assert website.active_configuration_version == website.configuration_version


@pytest.mark.asyncio
async def test_repreview_after_stale_edit_unblocks_approval(db_session, draft_website):
    from app.services.website_configuration import save_draft_configuration

    website, admin = draft_website
    db_session.commit()

    with patched_http_fetch(html_handler("jsonld_single_event.html")):
        await preview_extraction(db_session, website)
    save_draft_configuration(
        db_session, website, SiteConfiguration.model_validate(website.configuration)
    )
    with patched_http_fetch(html_handler("jsonld_single_event.html")):
        await preview_extraction(db_session, website)

    approve_configuration(db_session, website, approved_by_user_id=admin.id)
    assert website.approved_pattern is not None


# --- Browser-required / unsupported-pattern blocks ------------------------------------


@pytest.mark.asyncio
async def test_approval_blocked_when_latest_detection_flagged_browser_required(
    db_session, draft_website
):
    website, admin = draft_website
    website.proposed_pattern = {"detection": {"browser_required": True}, "configuration": None}
    db_session.commit()

    with patched_http_fetch(html_handler("jsonld_single_event.html")):
        await preview_extraction(db_session, website)

    with pytest.raises(AppError) as exc:
        approve_configuration(db_session, website, approved_by_user_id=admin.id)
    assert "browser rendering" in str(exc.value).lower()


def test_approval_blocked_for_unregistered_pattern_name(db_session, draft_website):
    website, admin = draft_website
    website.configuration = {
        "pattern_name": "not_a_real_pattern",
        "listing_url": "https://example.com/events",
        "config_version": 1,
    }
    db_session.commit()

    with pytest.raises(AppError):
        approve_configuration(db_session, website, approved_by_user_id=admin.id)


# --- City / archived checks -----------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_blocked_when_city_is_inactive(db_session, draft_website, make_city):
    website, admin = draft_website
    inactive_city = make_city(name="Inactive City", slug="inactive-city-approval", is_active=False)
    website.city_id = inactive_city.id
    db_session.commit()

    with patched_http_fetch(html_handler("jsonld_single_event.html")):
        await preview_extraction(db_session, website)

    with pytest.raises(AppError) as exc:
        approve_configuration(db_session, website, approved_by_user_id=admin.id)
    assert "active city" in str(exc.value).lower()


def test_approval_blocked_for_archived_website(db_session, draft_website):
    from datetime import UTC, datetime

    website, admin = draft_website
    website.onboarding_status = "archived"
    website.archived_at = datetime.now(UTC)
    db_session.commit()

    with pytest.raises(AppError) as exc:
        approve_configuration(db_session, website, approved_by_user_id=admin.id)
    assert "archived" in str(exc.value).lower()


# --- Reapproval ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reapproval_replaces_frozen_snapshot(db_session, draft_website):
    from app.services.website_configuration import save_draft_configuration

    website, admin = draft_website
    db_session.commit()

    with patched_http_fetch(html_handler("jsonld_single_event.html")):
        await preview_extraction(db_session, website)
    approve_configuration(db_session, website, approved_by_user_id=admin.id)
    first_snapshot_version = website.active_configuration_version

    save_draft_configuration(
        db_session,
        website,
        SiteConfiguration(pattern_name="json_ld_event", listing_url="https://example.com/v2"),
    )
    with patched_http_fetch(html_handler("jsonld_single_event.html")):
        await preview_extraction(db_session, website)
    approve_configuration(db_session, website, approved_by_user_id=admin.id)

    assert website.active_configuration_version != first_snapshot_version
    assert website.approved_pattern["listing_url"] == "https://example.com/v2"
    # An already-approved/active website is not re-run through the state
    # machine — it stays wherever it already was (approved, in this case).
    assert website.onboarding_status == "approved"


@pytest.mark.asyncio
async def test_reapproval_does_not_deactivate_an_active_website(db_session, draft_website):
    website, admin = draft_website
    db_session.commit()
    with patched_http_fetch(html_handler("jsonld_single_event.html")):
        await preview_extraction(db_session, website)
    approve_configuration(db_session, website, approved_by_user_id=admin.id)
    website.onboarding_status = "active"
    website.is_active = True
    db_session.commit()

    with patched_http_fetch(html_handler("jsonld_single_event.html")):
        await preview_extraction(db_session, website)
    approve_configuration(db_session, website, approved_by_user_id=admin.id)

    assert website.onboarding_status == "active"
    assert website.is_active is True


def test_approval_route_records_before_and_after_audit_history(
    client, make_super_admin, make_city, make_website, login, db_session
):
    import asyncio

    make_super_admin(email="reapprove-root@example.com", password="root-pass-1234")
    city = make_city(name="Reapprove City", slug="reapprove-city")
    website = make_website(city, name="Reapprove Site")
    website.configuration = JSONLD_CONFIG.model_dump(mode="json")
    db_session.commit()
    with patched_http_fetch(html_handler("jsonld_single_event.html")):
        asyncio.run(preview_extraction(db_session, website))
    login("reapprove-root@example.com", "root-pass-1234")

    resp = client.post(
        f"/admin/websites/{website.id}/approve-configuration",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    entries = db_session.query(AuditLog).filter(AuditLog.action == "configuration_approved").all()
    assert len(entries) == 1
    assert entries[0].after_state is not None


# --- Rejection ---------------------------------------------------------------------


def test_reject_configuration_requires_reason(db_session, draft_website):
    website, _admin = draft_website
    db_session.commit()

    with pytest.raises(AppError):
        reject_configuration(db_session, website, reason="   ")


def test_reject_initial_onboarding_clears_draft_and_moves_to_unsupported(db_session, draft_website):
    from app.services.websites import transition_website

    website, _admin = draft_website
    db_session.commit()
    transition_website(db_session, website, "detecting")
    transition_website(db_session, website, "detected")

    reject_configuration(db_session, website, reason="Selectors look wrong")

    assert website.configuration is None
    assert website.onboarding_status == "unsupported"
    assert website.approved_pattern is None


@pytest.mark.asyncio
async def test_reject_revised_draft_for_approved_website_preserves_approval_and_active_status(
    db_session, draft_website
):
    from app.services.website_configuration import save_draft_configuration

    website, admin = draft_website
    db_session.commit()
    with patched_http_fetch(html_handler("jsonld_single_event.html")):
        await preview_extraction(db_session, website)
    approve_configuration(db_session, website, approved_by_user_id=admin.id)
    approved_snapshot = dict(website.approved_pattern)
    website.onboarding_status = "active"
    website.is_active = True
    db_session.commit()

    save_draft_configuration(
        db_session,
        website,
        SiteConfiguration(pattern_name="json_ld_event", listing_url="https://example.com/bad"),
    )
    reject_configuration(db_session, website, reason="Bad revision, keep the old one")

    assert website.onboarding_status == "active"
    assert website.is_active is True
    assert website.approved_pattern == approved_snapshot
    # The draft falls back to the last approved snapshot, not the rejected edit.
    assert website.configuration == approved_snapshot


def test_reject_configuration_route_requires_sites_approve_permission(
    client, make_user, make_city, make_website, login, db_session
):
    from app.core.permissions import EDITOR

    city = make_city(name="Reject Denied City", slug="reject-denied-city")
    website = make_website(city, name="Reject Denied Site")
    website.configuration = JSONLD_CONFIG.model_dump(mode="json")
    db_session.commit()
    make_user(email="reject-editor@example.com", password="editor-pass-123", role_name=EDITOR)
    login("reject-editor@example.com", "editor-pass-123")

    resp = client.post(
        f"/admin/websites/{website.id}/reject-configuration",
        data={"reason": "no", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_reject_configuration_route_is_audited(
    client, make_super_admin, make_city, make_website, login, db_session
):
    make_super_admin(email="reject-root@example.com", password="root-pass-1234")
    city = make_city(name="Reject City", slug="reject-city")
    website = make_website(city, name="Reject Site")
    website.configuration = JSONLD_CONFIG.model_dump(mode="json")
    db_session.commit()
    login("reject-root@example.com", "root-pass-1234")

    resp = client.post(
        f"/admin/websites/{website.id}/reject-configuration",
        data={"reason": "Needs rework", "notes": "see selectors", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    entries = (
        db_session.query(AuditLog).filter(AuditLog.action == "website_configuration_rejected").all()
    )
    assert len(entries) == 1
    assert "Needs rework" in entries[0].detail


def test_blocked_preview_also_blocks_approval(db_session, draft_website):
    website, admin = draft_website
    db_session.commit()

    import asyncio

    with patched_http_fetch(blocked_handler(403)):
        asyncio.run(preview_extraction(db_session, website))

    with pytest.raises(AppError):
        approve_configuration(db_session, website, approved_by_user_id=admin.id)
