"""Admin automatic-import schedule: display, default on activation, editing,
and scheduler-process health — all on the existing durable scheduler.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.onboarding import ACTIVE, APPROVED
from app.services.schedule_admin import (
    DEFAULT_SCHEDULE_CONFIG,
    apply_schedule,
    build_schedule_config,
    build_website_schedule_view,
    describe_scheduler_process,
    ensure_default_schedule,
    format_admin_datetime,
    format_interval,
)
from app.services.scheduler import get_or_create_state
from app.services.websites import transition_website

pytestmark = pytest.mark.usefixtures("_reset_db")


def _approved(strategy="http") -> dict:
    return {
        "configuration": {
            "pattern_name": "simpleview_events",
            "api_endpoint": "https://example.com/x/find/",
            "execution_strategy": strategy,
            "json_paths": {"events_root": "docs.docs"},
        }
    }


def _site(make_website, city, db_session, *, name="Site", strategy="http", schedule=None,
          is_active=True, approved=True, status=ACTIVE):
    w = make_website(city, name=name, is_active=is_active,
                     approved_pattern=_approved(strategy) if approved else None)
    w.onboarding_status = status
    w.schedule_config = schedule
    db_session.commit()
    db_session.refresh(w)
    return w


# --- frequency formatting ----------------------------------------------------


@pytest.mark.parametrize(
    "minutes,expected",
    [
        (15, "Every 15 minutes"),
        (30, "Every 30 minutes"),
        (60, "Every hour"),
        (180, "Every 3 hours"),
        (720, "Every 12 hours"),
        (1440, "Every 24 hours"),
        (2880, "Every 2 days"),
        (10080, "Every 7 days"),
        (None, "—"),
    ],
)
def test_format_interval(minutes, expected):
    assert format_interval(minutes) == expected


def test_format_admin_datetime_is_timezone_aware():
    # A UTC instant is rendered in the configured application timezone, never a
    # naive UTC string.
    out = format_admin_datetime(datetime(2026, 8, 7, 20, 18, tzinfo=UTC))
    assert "2026" in out and ("AM" in out or "PM" in out)
    assert format_admin_datetime(None) == "—"


# --- default schedule on activation ------------------------------------------


def test_ensure_default_schedule_sets_daily_when_none(make_city, make_website, db_session):
    w = _site(make_website, make_city(), db_session, schedule=None)
    assert ensure_default_schedule(w) is True
    assert w.schedule_config == DEFAULT_SCHEDULE_CONFIG
    assert w.schedule_config["interval_minutes"] == 1440


def test_ensure_default_schedule_preserves_custom(make_city, make_website, db_session):
    custom = {"enabled": True, "interval_minutes": 60}
    w = _site(make_website, make_city(), db_session, schedule=custom)
    assert ensure_default_schedule(w) is False
    assert w.schedule_config == custom


def test_ensure_default_schedule_preserves_explicit_disable(make_city, make_website, db_session):
    disabled = {"enabled": False, "interval_minutes": 1440}
    w = _site(make_website, make_city(), db_session, schedule=disabled)
    # An admin who turned auto-imports off must not have them re-enabled.
    assert ensure_default_schedule(w) is False
    assert w.schedule_config["enabled"] is False


def test_ensure_default_schedule_ignores_unapproved_or_inactive(
    make_city, make_website, db_session
):
    city = make_city()
    assert ensure_default_schedule(_site(make_website, city, db_session, approved=False)) is False
    assert ensure_default_schedule(
        _site(make_website, city, db_session, is_active=False)
    ) is False


def test_activation_injects_default_schedule(make_city, make_website, db_session):
    # A website transitioning approved -> active with no schedule gets one.
    w = _site(make_website, make_city(), db_session, schedule=None, status=APPROVED,
              is_active=False)
    transition_website(db_session, w, ACTIVE)
    db_session.refresh(w)
    assert w.schedule_config == DEFAULT_SCHEDULE_CONFIG


def test_activation_does_not_reenable_disabled(make_city, make_website, db_session):
    w = _site(make_website, make_city(), db_session,
              schedule={"enabled": False, "interval_minutes": 1440},
              status=APPROVED, is_active=False)
    transition_website(db_session, w, ACTIVE)
    db_session.refresh(w)
    assert w.schedule_config["enabled"] is False


# --- schedule view -----------------------------------------------------------


def test_schedule_view_enabled(make_city, make_website, db_session):
    w = _site(make_website, make_city(), db_session,
              schedule={"enabled": True, "interval_minutes": 1440}, strategy="browser")
    view = build_website_schedule_view(db_session, w)
    assert view.status_label == "Enabled"
    assert view.frequency == "Every 24 hours"
    assert view.enabled is True
    assert view.execution == "Browser"


def test_schedule_view_disabled(make_city, make_website, db_session):
    w = _site(make_website, make_city(), db_session,
              schedule={"enabled": False, "interval_minutes": 1440})
    view = build_website_schedule_view(db_session, w)
    assert view.status_label == "Disabled"
    assert view.scheduler_state == "Disabled"
    assert view.frequency == "—"


def test_schedule_view_not_configured(make_city, make_website, db_session):
    w = _site(make_website, make_city(), db_session, schedule=None)
    view = build_website_schedule_view(db_session, w)
    assert view.status_label == "Not configured"
    assert "does not have an automatic import schedule" in view.status_detail


def test_schedule_view_overdue(make_city, make_website, db_session):
    w = _site(make_website, make_city(), db_session,
              schedule={"enabled": True, "interval_minutes": 1440})
    state = get_or_create_state(db_session, w.id)
    state.next_run_at = datetime.now(UTC) - timedelta(hours=1)
    state.paused = False
    db_session.commit()
    assert build_website_schedule_view(db_session, w).scheduler_state == "Overdue"


# --- editing + scoped reconcile ----------------------------------------------


def test_build_schedule_config_clamps_bounds():
    assert build_schedule_config(enabled=True, interval_minutes=5)["interval_minutes"] == 15
    assert build_schedule_config(enabled=True, interval_minutes=999999)["interval_minutes"] == (
        60 * 24 * 30
    )


def test_apply_schedule_enabled_sets_next_run_and_unpauses(make_city, make_website, db_session):
    w = _site(make_website, make_city(), db_session, schedule=None)
    state = get_or_create_state(db_session, w.id)
    state.paused = True
    db_session.commit()
    apply_schedule(db_session, w, build_schedule_config(enabled=True, interval_minutes=60))
    db_session.expire_all()
    after = get_or_create_state(db_session, w.id)
    assert after.paused is False
    assert after.next_run_at is not None  # scoped reconcile — no 5-minute wait


def test_apply_schedule_disabled_pauses(make_city, make_website, db_session):
    w = _site(make_website, make_city(), db_session,
              schedule={"enabled": True, "interval_minutes": 60})
    apply_schedule(db_session, w, build_schedule_config(enabled=False, interval_minutes=60))
    db_session.expire_all()
    assert get_or_create_state(db_session, w.id).paused is True


# --- scheduler process health ------------------------------------------------


def test_describe_scheduler_process_not_detected(db_session):
    status = describe_scheduler_process(db_session)
    assert status.status == "Not detected"
    assert "python -m app.scheduler" in status.detail
