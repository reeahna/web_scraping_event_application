import re

import pytest

from app.core.permissions import EDITOR
from app.extraction.unsupported import UnsupportedReportData
from app.repositories.unsupported_site_report import create_unsupported_site_report
from app.services.extraction_runs import run_detection
from tests.extraction_helpers import html_handler, patched_http_fetch


@pytest.mark.asyncio
async def test_onboarding_metrics_reflect_real_counts(
    client, make_super_admin, make_city, make_website, login, db_session
):
    make_super_admin(email="dash-root@example.com", password="root-pass-1234")
    city = make_city(name="Dashboard City", slug="dashboard-city")
    website = make_website(city, name="Dashboard Site")
    website.event_listing_url = "https://example.com/events"
    db_session.commit()

    with patched_http_fetch(html_handler("unsupported_page.html")):
        await run_detection(db_session, website)

    login("dash-root@example.com", "root-pass-1234")
    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "Broken" in resp.text
    assert "1</strong> unsupported website" in resp.text
    assert "1</strong> open failure report to triage" in resp.text
    assert "/admin/websites?onboarding_status=unsupported" in resp.text


def test_zero_count_statuses_are_not_rendered(client, make_super_admin, login):
    """The dashboard lists only non-zero statuses — an empty install should not
    render a row of permanent zeros."""
    make_super_admin(email="dash-empty@example.com", password="root-pass-1234")
    login("dash-empty@example.com", "root-pass-1234")

    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "Nothing is broken." in resp.text
    assert "Nothing is waiting on you." in resp.text
    assert "onboarding_status=draft" not in resp.text


def test_detected_website_is_waiting_on_a_human_not_in_progress(
    client, make_super_admin, make_city, make_website, login, db_session
):
    """DETECTED needs an operator to preview and approve (app.core.onboarding),
    so it belongs under "Waiting on you" — grouping it as in-progress work
    implied the system was still busy with it."""
    make_super_admin(email="dash-detected@example.com", password="root-pass-1234")
    city = make_city(name="Detected City", slug="detected-city")
    website = make_website(city, name="Detected Site")
    website.onboarding_status = "detected"
    db_session.commit()

    login("dash-detected@example.com", "root-pass-1234")
    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "Waiting on you" in resp.text
    assert "1</strong> website awaiting preview &amp; approval" in resp.text


def test_editor_sees_onboarding_metrics_but_not_audit_log(client, make_user, login):
    make_user(email="dash-editor@example.com", password="editor-pass-123", role_name=EDITOR)
    login("dash-editor@example.com", "editor-pass-123")

    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "Waiting on you" in resp.text
    assert "Recent audit actions" not in resp.text


def test_welcome_prefers_display_name_over_email(client, make_super_admin, db_session, login):
    admin = make_super_admin(email="dash-named@example.com", password="root-pass-1234")
    admin.full_name = "Reeahna Patel"
    db_session.commit()

    login("dash-named@example.com", "root-pass-1234")
    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "Welcome, Reeahna Patel" in resp.text


def test_welcome_falls_back_to_email_without_display_name(client, make_super_admin, login):
    make_super_admin(email="dash-unnamed@example.com", password="root-pass-1234")
    login("dash-unnamed@example.com", "root-pass-1234")

    resp = client.get("/admin")
    assert resp.status_code == 200
    assert "Welcome, dash-unnamed@example.com" in resp.text


def test_unread_notification_count_shown_on_dashboard(client, make_super_admin, db_session, login):
    from app.services.notifications import build_dedup_fingerprint, notify

    admin = make_super_admin(email="dash-notif-root@example.com", password="root-pass-1234")
    notify(
        db_session,
        notification_type="test_event",
        severity="info",
        title="Dashboard test",
        message="msg",
        recipients=[admin],
        dedup_fingerprint=build_dedup_fingerprint("dashboard_test", "1"),
    )
    login("dash-notif-root@example.com", "root-pass-1234")

    resp = client.get("/admin")
    assert resp.status_code == 200
    # The count lives in the nav, not a dashboard tile — one place, always visible.
    assert "Notifications (1)" in resp.text


def test_dashboard_queries_stay_bounded_count_only(db_session, make_city, make_website):
    """Sanity check: the report-count helper used by the dashboard performs a
    single COUNT query, never loading full report payloads."""
    city = make_city(name="Bounded City", slug="bounded-city")
    website = make_website(city, name="Bounded Site")
    create_unsupported_site_report(
        db_session,
        UnsupportedReportData(
            website_id=website.id,
            submitted_url="https://example.com/events",
            final_url=None,
            http_status=None,
            page_title=None,
            detected_platform_evidence={},
            available_detector_results={},
            discovered_endpoints=[],
            browser_required=False,
            json_ld_presence=False,
            pagination_indicators={},
            access_denied_or_challenge_detected=True,
            failure_reason="blocked",
            fingerprint="bounded-fp",
        ),
    )
    from app.repositories.unsupported_site_report import count_unresolved_reports

    assert count_unresolved_reports(db_session) == 1


def test_dashboard_counts_universities(
    client, make_super_admin, make_city, login, db_session
):
    """Sits beside cities, websites and events, and counts the same way they
    do: every row, including towns that are inactive."""
    make_super_admin(email="dash-unis@example.com", password="root-pass-1234")
    make_city(
        name="Claremont", slug="claremont-ca",
        universities=["Pomona College", "Harvey Mudd College"],
    )
    make_city(name="Bloomington", slug="bloomington-in", universities=["Indiana University"])
    make_city(name="Nowhere", slug="nowhere-xx")
    db_session.commit()

    login("dash-unis@example.com", "root-pass-1234")
    html = client.get("/admin").text
    assert ">Universities</div>" in html
    assert '<div class="metric-value">3</div>' in html


def test_the_universities_tile_reads_zero_with_none(
    client, make_super_admin, make_city, login, db_session
):
    make_super_admin(email="dash-nouni@example.com", password="root-pass-1234")
    make_city(name="Plain", slug="plain-town")
    db_session.commit()

    login("dash-nouni@example.com", "root-pass-1234")
    html = client.get("/admin").text
    assert ">Universities</div>" in html


def test_audit_timestamps_are_formatted_not_raw(
    client, make_super_admin, login
):
    """These rendered as "2026-10-05 22:42:54.337938" on the dashboard and the
    full audit log, after the same fix had been applied elsewhere."""
    import re

    make_super_admin(email="dash-stamp@example.com", password="root-pass-1234")
    login("dash-stamp@example.com", "root-pass-1234")

    raw = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+")
    for path in ("/admin", "/admin/audit"):
        html = client.get(path).text
        assert raw.search(html) is None, path
        assert " at " in html, path


def test_dashboard_shows_the_ai_label_queue(
    client, make_super_admin, make_city, make_event, login, monkeypatch
):
    from datetime import date, timedelta

    from app.config import get_settings
    from app.services.ai_categorizer import PROMPT_VERSION

    monkeypatch.setattr(get_settings(), "groq_api_key", "test-key")
    make_super_admin(email="dash-ai@example.com", password="root-pass-1234")
    city = make_city(name="AI Queue City", slug="ai-queue-city")
    soon = date.today() + timedelta(days=3)
    for n in range(2):
        make_event(city, title=f"Waiting {n}", canonical_url=f"https://e.x/w{n}", start_date=soon)
    make_event(
        city,
        title="Labeled",
        canonical_url="https://e.x/l",
        start_date=soon,
        category_source="ai",
        ai_prompt_version=PROMPT_VERSION,
    )
    # Past events are never sent to the AI, so they are not waiting.
    make_event(
        city, title="Past", canonical_url="https://e.x/p", start_date=date(2020, 1, 1)
    )

    login("dash-ai@example.com", "root-pass-1234")
    resp = client.get("/admin")
    assert '<div class="metric-value">2</div>' in resp.text
    assert "Waiting for AI labels" in resp.text
    assert "1 upcoming event labeled" in resp.text


def test_dashboard_hides_the_ai_queue_when_ai_is_off(
    client, make_super_admin, login, monkeypatch
):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "groq_api_key", None)
    monkeypatch.setattr(settings, "gemini_api_key", None)
    make_super_admin(email="dash-noai@example.com", password="root-pass-1234")
    login("dash-noai@example.com", "root-pass-1234")
    assert "Waiting for AI labels" not in client.get("/admin").text


def test_dashboard_counts_use_thousands_separators():
    from app.core.templating import templates

    thousands = templates.env.filters["thousands"]
    assert thousands(12345) == "12,345"
    assert thousands(0) == "0"
    assert thousands(None) == "0"


def test_cities_websites_and_events_tiles_open_their_admin_pages(client, make_user, login):
    from app.core.permissions import ADMINISTRATOR

    make_user(email="tiles@example.com", role_name=ADMINISTRATOR)
    login("tiles@example.com")
    html = client.get("/admin").text
    for href, label in (
        ("/admin/cities?status=active", "Active cities"),
        ("/admin/websites", "Websites"),
        ("/admin/events", "Events"),
    ):
        tile = re.search(
            rf'<a class="metric-tile metric-tile-link" href="{re.escape(href)}">(.*?)</a>',
            html,
            re.S,
        )
        assert tile, href
        assert f">{label}</div>" in tile.group(1)
        assert client.get(href).status_code == 200
    # Universities has no page of its own, so its tile stays plain.
    assert 'metric-tile-link" href="/admin/universities' not in html


def test_a_tile_is_not_a_link_without_permission_to_open_its_page(
    client, make_user, login, monkeypatch
):
    """A custom role can see the dashboard without events.view; its Events
    tile then stays a plain number instead of a link to a Forbidden page."""
    from app.core.permissions import ADMINISTRATOR
    from app.routers import admin as admin_router

    real = admin_router.user_has_permission
    monkeypatch.setattr(
        admin_router,
        "user_has_permission",
        lambda db, user, code: code != "events.view" and real(db, user, code),
    )
    make_user(email="tiles-partial@example.com", role_name=ADMINISTRATOR)
    login("tiles-partial@example.com")
    html = client.get("/admin").text
    assert 'href="/admin/websites">' in html
    assert 'metric-tile-link" href="/admin/events"' not in html
    assert ">Events</div>" in html
