"""Phase 13: alert generation, delivery, dedup, reminders, and digests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.permissions import REGISTERED_USER
from app.models.user_engagement import AlertDelivery
from app.services import alerts, engagement
from app.services.email import MemoryEmailSender


def _user(make_user, n):
    return make_user(email=f"a{n}@example.com", password="password12345", role_name=REGISTERED_USER)


def _deliveries(db, user_id):
    return db.query(AlertDelivery).filter(AlertDelivery.user_id == user_id).all()


def test_new_event_alerts_city_followers(
    make_user, make_city, make_website, make_event, db_session
):
    user = _user(make_user, 1)
    city = make_city()
    website = make_website(city, is_active=True, approved_pattern={"pattern_name": "static_html"})
    engagement.follow(db_session, user_id=user.id, follow_type="city", target_id=city.id)
    engagement.update_preferences(db_session, user_id=user.id, values={"email_enabled": True})

    event = make_event(city, website=website, title="New Show")
    sender = MemoryEmailSender()
    count = alerts.on_new_event(db_session, event, sender=sender)
    assert count == 1
    channels = {d.channel for d in _deliveries(db_session, user.id)}
    assert channels == {"in_app", "email"}
    assert len(sender.sent) == 1  # immediate email dispatched

    # Second call is a no-op (dedup by the delivery ledger).
    sender2 = MemoryEmailSender()
    assert alerts.on_new_event(db_session, event, sender=sender2) == 0
    assert sender2.sent == []


def test_new_event_respects_preference_off(
    make_user, make_city, make_website, make_event, db_session
):
    user = _user(make_user, 1)
    city = make_city()
    website = make_website(city, is_active=True, approved_pattern={"pattern_name": "static_html"})
    engagement.follow(db_session, user_id=user.id, follow_type="city", target_id=city.id)
    engagement.update_preferences(db_session, user_id=user.id, values={"notify_new_events": False})
    event = make_event(city, website=website, title="Show")
    assert alerts.on_new_event(db_session, event, sender=MemoryEmailSender()) == 0


def test_digest_batches_pending_email(make_user, make_city, make_website, make_event, db_session):
    user = _user(make_user, 1)
    city = make_city()
    website = make_website(city, is_active=True, approved_pattern={"pattern_name": "static_html"})
    engagement.follow(db_session, user_id=user.id, follow_type="city", target_id=city.id)
    engagement.update_preferences(
        db_session, user_id=user.id, values={"email_enabled": True, "frequency": "daily"}
    )
    e1 = make_event(city, website=website, title="E1")
    e2 = make_event(city, website=website, title="E2", canonical_url="https://x/2")
    sender = MemoryEmailSender()
    for event in (e1, e2):
        alerts.on_new_event(db_session, event, sender=sender)
    # Immediate email did NOT fire (daily digest); emails are pending.
    assert sender.sent == []
    pending = [d for d in _deliveries(db_session, user.id) if d.channel == "email"]
    assert pending and all(d.status == "pending" for d in pending)

    # Digest sends one combined email and marks them sent.
    sent = alerts.send_pending_digests(db_session, now=datetime.now(UTC), sender=sender)
    assert sent == 1
    assert len(sender.sent) == 1
    assert all(d.status == "sent" for d in _deliveries(db_session, user.id) if d.channel == "email")


def test_cancellation_alerts_savers(make_user, make_city, make_website, make_event, db_session):
    user = _user(make_user, 1)
    city = make_city()
    website = make_website(city, is_active=True, approved_pattern={"pattern_name": "static_html"})
    event = make_event(city, website=website, title="Doomed")
    engagement.save_event(db_session, user_id=user.id, event_id=event.id)
    count = alerts.on_event_change(db_session, event, kind="cancelled", sender=MemoryEmailSender())
    assert count == 1
    assert any(d.alert_key.startswith("cancelled:") for d in _deliveries(db_session, user.id))


def test_saved_event_reminder(make_user, make_city, make_website, make_event, db_session):
    user = _user(make_user, 1)
    city = make_city()
    website = make_website(city, is_active=True, approved_pattern={"pattern_name": "static_html"})
    now = datetime(2026, 6, 1, tzinfo=UTC)
    tomorrow = now.date() + timedelta(days=1)
    event = make_event(city, website=website, title="Tomorrow Fest", start_date=tomorrow)
    engagement.save_event(db_session, user_id=user.id, event_id=event.id)
    count = alerts.send_saved_event_reminders(db_session, now=now, sender=MemoryEmailSender())
    assert count == 1
    assert any(d.alert_key.startswith("reminder:") for d in _deliveries(db_session, user.id))
    # Deduped: running again does nothing new.
    assert alerts.send_saved_event_reminders(db_session, now=now, sender=MemoryEmailSender()) == 0
