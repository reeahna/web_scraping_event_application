"""Source health is separate from event visibility.

Three failed imports in a row move a source to FAILING: its imports pause and
administrators are told, but its events stay public (each still subject to its
own visibility rules), the town's freshness line keeps counting it and says it
could not be checked, and after a day without a successful check its events
are labelled "May be out of date". A successful import after an administrator
resumes it clears all of that. Nothing about a source failing ever archives,
deactivates or deletes an event.
"""

import re
from datetime import UTC, datetime, timedelta

import pytest

from app.core.onboarding import ACTIVE, ARCHIVED, FAILING, INACTIVE
from app.models.event import Event
from app.models.extraction_run import ExtractionRun
from app.models.notification import Notification
from app.repositories.public_events import (
    current_public_date,
    get_public_event,
    list_public_events,
    local_today,
)
from app.services.event_freshness import (
    source_health,
    stale_source_ids,
    town_listings_checked,
)
from app.services.extraction_runs import CONSECUTIVE_FAILURE_THRESHOLD, _update_website_health
from app.services.scheduler import _source_eligibility_reasons
from app.services.websites import transition_website

APPROVED = {"pattern_name": "json_ld_event", "listing_url": "https://example.com/events"}
DAILY = {"enabled": True, "interval_minutes": 1440}
OFF = {"enabled": False, "interval_minutes": 1440}


@pytest.fixture
def town(make_city):
    return make_city(name="Tuscaloosa", slug="tuscaloosa", timezone="America/Chicago")


def _source(db_session, make_website, city, name="library", *, status=ACTIVE, schedule=DAILY,
            display=None):
    site = make_website(
        city, name=name, base_url=f"https://{name}.example.com", is_active=status == ACTIVE,
        approved_pattern=APPROVED, source_display_name=display,
    )
    site.onboarding_status = status
    site.schedule_config = schedule
    db_session.commit()
    return site


def _run(db_session, site, finished, status="success"):
    db_session.add(ExtractionRun(
        website_id=site.id, run_type="manual", status=status, source_url=site.base_url,
        started_at=finished - timedelta(minutes=1), completed_at=finished,
    ))
    db_session.commit()


def _event(make_event, city, site, slug, *, days=3, **values):
    return make_event(
        city, title=f"Story time {slug}", website=site,
        canonical_url=f"https://library.example.com/{slug}",
        start_date=current_public_date() + timedelta(days=days), **values,
    )


def _public_ids(db_session, city):
    events, _, _ = list_public_events(
        db_session, today=local_today(db_session), page=1, city_id=city.id
    )
    return {event.id for event in events}


def _fail_until_failing(db_session, site):
    for _ in range(CONSECUTIVE_FAILURE_THRESHOLD):
        _run(db_session, site, datetime.now(UTC), status="blocked")
        _update_website_health(
            db_session, site, "blocked", blocked_reason="http_403", error_summary="HTTP 403"
        )


def _text(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


# --- The failure transition ----------------------------------------------------------


def test_three_failures_pause_imports_and_notify_but_keep_events_public(
    db_session, make_website, make_event, make_super_admin, town
):
    make_super_admin()
    site = _source(db_session, make_website, town)
    _run(db_session, site, datetime.now(UTC) - timedelta(days=2))
    events = [_event(make_event, town, site, f"e{i}") for i in range(3)]
    assert _public_ids(db_session, town) == {e.id for e in events}

    _fail_until_failing(db_session, site)

    assert site.onboarding_status == FAILING
    assert _source_eligibility_reasons(site), "imports must be paused"
    notice = db_session.query(Notification).filter_by(
        notification_type="website_entered_failing_status"
    ).first()
    assert notice is not None and "events stay public" in notice.message
    # Every event is still public and untouched: never archived, deactivated
    # or deleted because its source failed.
    assert _public_ids(db_session, town) == {e.id for e in events}
    for event in events:
        db_session.refresh(event)
        assert event.is_active and event.archived_at is None
    assert db_session.query(Event).count() == 3


def test_failing_source_events_keep_their_own_visibility_rules(
    db_session, make_website, make_event, town
):
    site = _source(db_session, make_website, town, status=FAILING)
    shown = _event(make_event, town, site, "shown")
    hidden = [
        _event(make_event, town, site, "ended", days=-3),
        _event(make_event, town, site, "archived", archived=True),
        _event(make_event, town, site, "deactivated", is_active=False),
        _event(make_event, town, site, "dup", duplicate_status="confirmed_duplicate"),
    ]
    assert _public_ids(db_session, town) == {shown.id}
    today = local_today(db_session)
    assert get_public_event(db_session, shown.id, today=today) is not None
    for event in hidden:
        assert get_public_event(db_session, event.id, today=today) is None


@pytest.mark.parametrize("status", [INACTIVE, ARCHIVED])
def test_sources_an_administrator_turned_off_still_hide_their_events(
    db_session, make_website, make_event, town, status
):
    site = _source(db_session, make_website, town, status=status)
    if status == ARCHIVED:
        site.archived_at = datetime.now(UTC)
        db_session.commit()
    event = _event(make_event, town, site, "x")
    assert event.id not in _public_ids(db_session, town)


def test_a_failing_source_can_still_be_followed(
    client, db_session, make_website, make_user, login, town
):
    from app.routers.engagement import _followable

    site = _source(db_session, make_website, town, status=FAILING)
    assert _followable(db_session, "source", site.id)
    off = _source(db_session, make_website, town, "off", status=INACTIVE)
    assert not _followable(db_session, "source", off.id)


# --- Stale label -------------------------------------------------------------------


def test_stale_only_after_a_day_without_a_successful_check(db_session, make_website, town):
    now = datetime.now(UTC)
    recent = _source(db_session, make_website, town, "recent", status=FAILING)
    _run(db_session, recent, now - timedelta(hours=20))
    old = _source(db_session, make_website, town, "old", status=FAILING)
    _run(db_session, old, now - timedelta(hours=30))
    healthy = _source(db_session, make_website, town, "healthy")
    _run(db_session, healthy, now - timedelta(hours=23))
    # Paused on purpose: updated by hand, so never labelled.
    paused = _source(db_session, make_website, town, "paused", schedule=OFF)
    _run(db_session, paused, now - timedelta(days=10))
    # Scheduled and active, but nothing has succeeded for days: overdue.
    overdue = _source(db_session, make_website, town, "overdue")
    _run(db_session, overdue, now - timedelta(days=3))
    ids = [s.id for s in (recent, old, healthy, paused, overdue)]
    assert stale_source_ids(db_session, ids, now=now) == {old.id, overdue.id}
    health = source_health(db_session, [paused, recent])
    assert health[paused.id].paused and not health[paused.id].failing
    assert health[recent.id].failing and not health[recent.id].stale


def test_stale_events_are_labelled_on_cards_and_event_pages(
    client, db_session, make_website, make_event, town
):
    site = _source(db_session, make_website, town, status=FAILING, display="Public Library")
    _run(db_session, site, datetime(2026, 11, 23, 22, 7, tzinfo=UTC))
    event = _event(make_event, town, site, "e1")
    fresh_site = _source(db_session, make_website, town, "fresh")
    _run(db_session, fresh_site, datetime.now(UTC) - timedelta(hours=1))
    fresh = _event(make_event, town, fresh_site, "e2")

    page = client.get("/city/tuscaloosa").text
    assert page.count("May be out of date") == 1
    detail = _text(client.get(f"/events/{event.id}").text)
    assert (
        "May be out of date. We could not check this event's source since "
        "November 23, 2026 at 4:07 PM CST" in detail
    )
    assert "May be out of date" not in client.get(f"/events/{fresh.id}").text


def test_resuming_and_a_successful_import_clears_the_warnings(
    client, db_session, make_website, make_event, make_super_admin, town
):
    make_super_admin()
    site = _source(db_session, make_website, town, display="Public Library")
    _run(db_session, site, datetime.now(UTC) - timedelta(days=2))
    event = _event(make_event, town, site, "e1")
    _fail_until_failing(db_session, site)

    text = _text(client.get("/city/tuscaloosa").text)
    assert "Public Library could not be checked on the latest try" in text
    assert "May be out of date" in text

    transition_website(db_session, site, ACTIVE)
    assert not _source_eligibility_reasons(site)
    _run(db_session, site, datetime.now(UTC))
    _update_website_health(db_session, site, "success")
    assert site.consecutive_failure_count == 0

    text = _text(client.get("/city/tuscaloosa").text)
    assert "could not be checked" not in text
    assert "May be out of date" not in text
    assert "May be out of date" not in client.get(f"/events/{event.id}").text


# --- Town freshness line -----------------------------------------------------------


def test_failing_source_keeps_counting_toward_the_town_time(
    client, db_session, make_website, make_event, town
):
    now = datetime.now(UTC)
    failing = _source(db_session, make_website, town, "library", status=FAILING,
                      display="Public Library")
    _run(db_session, failing, now - timedelta(days=1))
    _run(db_session, failing, now - timedelta(hours=2), status="blocked")
    good = _source(db_session, make_website, town, "city")
    _run(db_session, good, now - timedelta(hours=1))

    freshness = town_listings_checked(db_session, town.id)
    assert freshness.oldest_website_id == failing.id
    assert freshness.held_back_by == "failing"
    text = _text(client.get("/city/tuscaloosa").text)
    assert "Public Library could not be checked on the latest try" in text
    assert "Every other source was checked by" in text


def test_town_never_reads_current_while_an_expected_source_is_failing(
    client, db_session, make_website, town
):
    # The failing source is not the oldest: still said.
    now = datetime.now(UTC)
    old = _source(db_session, make_website, town, "old")
    _run(db_session, old, now - timedelta(hours=10))
    failing = _source(db_session, make_website, town, "failing", status=FAILING)
    _run(db_session, failing, now - timedelta(hours=5))
    _run(db_session, failing, now - timedelta(hours=4), status="failed")
    text = _text(client.get("/city/tuscaloosa").text)
    assert "1 source could not be checked on the latest try" in text


def test_paused_and_failing_sources_are_told_apart(
    client, db_session, make_website, town
):
    now = datetime.now(UTC)
    paused = _source(db_session, make_website, town, "paused", schedule=OFF, display="By Hand")
    _run(db_session, paused, now - timedelta(days=5))
    failing = _source(db_session, make_website, town, "failing", status=FAILING,
                      display="Broken")
    _run(db_session, failing, now - timedelta(days=2))
    good = _source(db_session, make_website, town, "good")
    _run(db_session, good, now - timedelta(hours=1))

    freshness = town_listings_checked(db_session, town.id)
    assert freshness.oldest_website_id == failing.id
    assert freshness.held_back_by == "failing"
    assert freshness.paused == 1
    text = _text(client.get("/city/tuscaloosa").text)
    assert "Broken could not be checked" in text
    assert "1 source updated by hand is not included in this time" in text
    assert "By Hand" not in text
