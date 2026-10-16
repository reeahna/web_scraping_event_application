"""The signed-in user's alerts inbox (/account/inbox)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.formatting import time_ago
from app.core.permissions import REGISTERED_USER
from app.services import user_alerts
from app.services.notifications import SEVERITY_INFO, notify

TOMORROW = datetime.now(UTC).date() + timedelta(days=1)


def _alert(db, user, *, title, event=None, kind="user_alert"):
    return notify(
        db,
        notification_type=kind,
        severity=SEVERITY_INFO,
        title=title,
        message=f"{title} body",
        recipients=[user],
        dedup_fingerprint=f"test:{user.id}:{title}",
        related_resource_type="event" if event else None,
        related_resource_id=event.id if event else None,
        action_url=f"/events/{event.id}" if event else None,
    )[0]


def _public_event(make_city, make_website, make_event, **over):
    city = make_city()
    website = make_website(city, is_active=True, approved_pattern={"pattern_name": "static_html"})
    return city, website, make_event(city, website=website, **over)


def test_inbox_lists_only_own_user_alerts(client, make_user, login, db_session):
    me = make_user(email="me@example.com", role_name=REGISTERED_USER)
    other = make_user(email="other@example.com", role_name=REGISTERED_USER)
    _alert(db_session, me, title="Mine")
    _alert(db_session, other, title="Theirs")
    _alert(db_session, me, title="Ops notice", kind="scrape_failure")
    login("me@example.com")
    page = client.get("/account/inbox")
    assert page.status_code == 200
    assert "Mine" in page.text
    assert "Theirs" not in page.text
    assert "Ops notice" not in page.text
    # The nav badge counts the one unread alert.
    assert "(1 unread)" in page.text


def test_inbox_requires_login(client):
    resp = client.get("/account/inbox", follow_redirects=False)
    assert resp.status_code in (302, 303, 401, 403)


def test_open_marks_read_and_redirects_to_event(
    client, make_user, login, db_session, make_city, make_website, make_event
):
    _, _, event = _public_event(make_city, make_website, make_event, start_date=TOMORROW)
    me = make_user(email="me@example.com", role_name=REGISTERED_USER)
    alert = _alert(db_session, me, title="New show", event=event)
    login("me@example.com")
    csrf = client.cookies.get("csrf_token")
    resp = client.post(f"/account/inbox/{alert.id}/open", data={"csrf_token": csrf},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/events/{event.id}"
    assert user_alerts.unread_count(db_session, user_id=me.id) == 0


def test_alert_for_past_event_is_greyed_and_not_linked(
    client, make_user, login, db_session, make_city, make_website, make_event
):
    _, _, event = _public_event(
        make_city, make_website, make_event, start_date=TOMORROW - timedelta(days=7)
    )
    me = make_user(email="me@example.com", role_name=REGISTERED_USER)
    alert = _alert(db_session, me, title="Old show", event=event)
    login("me@example.com")
    page = client.get("/account/inbox").text
    assert "This event has already happened" in page
    assert f"/account/inbox/{alert.id}/open" not in page
    assert f"/account/inbox/{alert.id}/read" in page


def test_cannot_touch_someone_elses_alert(client, make_user, login, db_session):
    other = make_user(email="other@example.com", role_name=REGISTERED_USER)
    theirs = _alert(db_session, other, title="Theirs")
    make_user(email="me@example.com", role_name=REGISTERED_USER)
    login("me@example.com")
    csrf = client.cookies.get("csrf_token")
    for action in ("open", "read", "dismiss"):
        resp = client.post(f"/account/inbox/{theirs.id}/{action}", data={"csrf_token": csrf},
                           follow_redirects=False)
        assert resp.status_code == 404
    db_session.refresh(theirs)
    assert theirs.read_at is None and theirs.dismissed_at is None


def test_dismiss_and_mark_all_read(client, make_user, login, db_session):
    me = make_user(email="me@example.com", role_name=REGISTERED_USER)
    first = _alert(db_session, me, title="First")
    _alert(db_session, me, title="Second")
    _alert(db_session, me, title="Third")
    login("me@example.com")
    csrf = client.cookies.get("csrf_token")
    client.post(f"/account/inbox/{first.id}/dismiss", data={"csrf_token": csrf})
    page = client.get("/account/inbox").text
    assert "First" not in page and "Second" in page
    client.post("/account/inbox/read-all", data={"csrf_token": csrf})
    assert user_alerts.unread_count(db_session, user_id=me.id) == 0


def test_nav_alerts_link_points_to_inbox(client, make_user, login):
    make_user(email="me@example.com", role_name=REGISTERED_USER)
    login("me@example.com")
    page = client.get("/account/saved").text
    assert 'href="/account/inbox"' in page


def test_time_ago():
    now = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
    assert time_ago(now - timedelta(seconds=10), now) == "just now"
    assert time_ago(now - timedelta(minutes=1), now) == "1 minute ago"
    assert time_ago(now - timedelta(hours=3), now) == "3 hours ago"
    assert time_ago(now - timedelta(days=1, hours=2), now) == "yesterday"
    assert time_ago(now - timedelta(days=4), now) == "4 days ago"
    assert time_ago(now - timedelta(days=30), now) == "Wed, Sep 16, 2026"
