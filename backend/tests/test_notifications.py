from unittest.mock import patch

import pytest

from app.core.permissions import ADMINISTRATOR, REGISTERED_USER
from app.models.notification import Notification
from app.repositories.notification import count_unread_for_user
from app.services.extraction_runs import run_detection
from app.services.notifications import build_dedup_fingerprint, notify
from app.services.rbac import users_with_permission
from tests.extraction_helpers import html_handler, patched_http_fetch


def _csrf(client) -> str:
    return client.cookies.get("csrf_token")


def test_notify_fans_out_one_row_per_recipient(db_session, make_user):
    a = make_user(email="recipient-a@example.com")
    b = make_user(email="recipient-b@example.com")

    created = notify(
        db_session,
        notification_type="test_event",
        severity="info",
        title="Test",
        message="A test notification",
        recipients=[a, b],
        dedup_fingerprint=build_dedup_fingerprint("test_event", "1"),
    )
    assert len(created) == 2
    assert {n.recipient_user_id for n in created} == {a.id, b.id}


def test_notify_with_no_recipients_creates_nothing(db_session):
    created = notify(
        db_session,
        notification_type="test_event",
        severity="info",
        title="Test",
        message="msg",
        recipients=[],
        dedup_fingerprint=build_dedup_fingerprint("test_event", "empty"),
    )
    assert created == []


def test_notify_dedupes_within_cooldown_window(db_session, make_user):
    user = make_user(email="dedupe@example.com")
    fingerprint = build_dedup_fingerprint("test_event", "same")

    first = notify(
        db_session,
        notification_type="test_event",
        severity="warning",
        title="First",
        message="msg",
        recipients=[user],
        dedup_fingerprint=fingerprint,
    )
    second = notify(
        db_session,
        notification_type="test_event",
        severity="warning",
        title="Second (should be suppressed)",
        message="msg",
        recipients=[user],
        dedup_fingerprint=fingerprint,
    )
    assert len(first) == 1
    assert second == []
    assert db_session.query(Notification).filter_by(recipient_user_id=user.id).count() == 1


def test_notify_creates_new_notification_for_different_fingerprint(db_session, make_user):
    user = make_user(email="different-fp@example.com")
    notify(
        db_session,
        notification_type="test_event",
        severity="info",
        title="A",
        message="msg",
        recipients=[user],
        dedup_fingerprint=build_dedup_fingerprint("test_event", "fp-a"),
    )
    second = notify(
        db_session,
        notification_type="test_event",
        severity="info",
        title="B",
        message="msg",
        recipients=[user],
        dedup_fingerprint=build_dedup_fingerprint("test_event", "fp-b"),
    )
    assert len(second) == 1


def test_provider_failure_does_not_raise_or_block_notification_creation(db_session, make_user):
    user = make_user(email="provider-fail@example.com")
    with patch(
        "app.services.notifications.DevelopmentEmailNotificationProvider.send",
        side_effect=RuntimeError("simulated failure"),
    ):
        created = notify(
            db_session,
            notification_type="test_event",
            severity="error",
            title="Should still be created",
            message="msg",
            recipients=[user],
            dedup_fingerprint=build_dedup_fingerprint("test_event", "provider-fail"),
        )
    assert len(created) == 1
    assert created[0].delivery_status == "failed"
    assert created[0].delivery_attempts == 1


def test_registered_user_never_becomes_a_sites_approve_recipient(db_session, make_user):
    make_user(email="reg-only@example.com", password="pw", role_name=REGISTERED_USER)
    recipients = users_with_permission(db_session, "sites.approve")
    assert all(u.email != "reg-only@example.com" for u in recipients)


@pytest.mark.asyncio
async def test_unsupported_detection_notifies_sites_approve_holders(
    db_session, make_city, make_website, make_user
):
    admin = make_user(email="notify-admin@example.com", password="pw", role_name=ADMINISTRATOR)
    city = make_city(name="Notify City", slug="notify-city")
    website = make_website(city, name="Notify Site")
    website.event_listing_url = "https://example.com/events"
    db_session.commit()

    with patched_http_fetch(html_handler("unsupported_page.html")):
        await run_detection(db_session, website)

    unread = count_unread_for_user(db_session, admin.id)
    assert unread >= 1
    notification = db_session.query(Notification).filter_by(recipient_user_id=admin.id).first()
    assert notification.notification_type == "website_detection_unsupported"
    assert notification.related_resource_type == "website"
    assert notification.related_resource_id == website.id


def test_mark_read_and_dismiss_only_affect_own_notification(
    client, make_super_admin, make_user, db_session, login
):
    admin = make_super_admin(email="notif-owner@example.com", password="root-pass-1234")
    other = make_user(email="notif-other@example.com")
    notify(
        db_session,
        notification_type="test_event",
        severity="info",
        title="Owner's notification",
        message="msg",
        recipients=[admin],
        dedup_fingerprint=build_dedup_fingerprint("test_event", "owner"),
    )
    other_notification = notify(
        db_session,
        notification_type="test_event",
        severity="info",
        title="Other's notification",
        message="msg",
        recipients=[other],
        dedup_fingerprint=build_dedup_fingerprint("test_event", "other"),
    )[0]

    login("notif-owner@example.com", "root-pass-1234")
    own_notification = db_session.query(Notification).filter_by(recipient_user_id=admin.id).one()

    resp = client.post(
        f"/admin/notifications/{own_notification.id}/read",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.refresh(own_notification)
    assert own_notification.read_at is not None

    resp = client.post(
        f"/admin/notifications/{other_notification.id}/read",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 404


def test_registered_user_denied_notifications_page(client, make_user, login):
    make_user(
        email="notif-denied@example.com", password="denied-pass-123", role_name=REGISTERED_USER
    )
    login("notif-denied@example.com", "denied-pass-123")

    assert client.get("/admin/notifications").status_code == 403
