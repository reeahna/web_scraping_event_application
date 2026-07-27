"""Phase 13: saved events, follows, and alert preferences (service layer)."""

from __future__ import annotations

from app.core.permissions import REGISTERED_USER
from app.services import engagement


def _user(make_user, n):
    return make_user(email=f"u{n}@example.com", password="password12345", role_name=REGISTERED_USER)


def test_save_and_unsave_is_idempotent(make_user, make_city, make_event, db_session):
    user = _user(make_user, 1)
    city = make_city()
    event = make_event(city, title="E1")
    engagement.save_event(db_session, user_id=user.id, event_id=event.id)
    engagement.save_event(db_session, user_id=user.id, event_id=event.id)  # no duplicate
    assert engagement.is_event_saved(db_session, user_id=user.id, event_id=event.id)
    assert [e.id for e in engagement.list_saved_events(db_session, user_id=user.id)] == [event.id]
    engagement.unsave_event(db_session, user_id=user.id, event_id=event.id)
    assert not engagement.is_event_saved(db_session, user_id=user.id, event_id=event.id)


def test_saved_events_are_private_between_users(make_user, make_city, make_event, db_session):
    a, b = _user(make_user, 1), _user(make_user, 2)
    city = make_city()
    event = make_event(city, title="E1")
    engagement.save_event(db_session, user_id=a.id, event_id=event.id)
    assert engagement.list_saved_events(db_session, user_id=a.id)
    assert engagement.list_saved_events(db_session, user_id=b.id) == []


def test_follow_and_followers(make_user, make_city, db_session):
    a, b = _user(make_user, 1), _user(make_user, 2)
    city = make_city()
    engagement.follow(db_session, user_id=a.id, follow_type="city", target_id=city.id)
    engagement.follow(db_session, user_id=a.id, follow_type="city", target_id=city.id)  # dup no-op
    engagement.follow(db_session, user_id=b.id, follow_type="city", target_id=city.id)
    followers = engagement.followers_of(db_session, follow_type="city", target_id=city.id)
    assert set(followers) == {a.id, b.id}
    engagement.unfollow(db_session, user_id=a.id, follow_type="city", target_id=city.id)
    assert engagement.followers_of(db_session, follow_type="city", target_id=city.id) == [b.id]


def test_preferences_created_with_unsubscribe_token(make_user, db_session):
    user = _user(make_user, 1)
    prefs = engagement.get_or_create_preferences(db_session, user_id=user.id)
    assert prefs.unsubscribe_token
    # Idempotent — same row returned.
    again = engagement.get_or_create_preferences(db_session, user_id=user.id)
    assert again.id == prefs.id


def test_update_and_unsubscribe(make_user, db_session):
    user = _user(make_user, 1)
    prefs = engagement.get_or_create_preferences(db_session, user_id=user.id)
    engagement.update_preferences(
        db_session, user_id=user.id, values={"email_enabled": True, "frequency": "daily"}
    )
    db_session.refresh(prefs)
    assert prefs.email_enabled is True
    assert prefs.frequency == "daily"

    assert engagement.unsubscribe_email(db_session, prefs.unsubscribe_token) is True
    db_session.refresh(prefs)
    assert prefs.email_enabled is False
    assert engagement.unsubscribe_email(db_session, "bogus") is False


def test_invalid_frequency_is_ignored(make_user, db_session):
    user = _user(make_user, 1)
    engagement.update_preferences(db_session, user_id=user.id, values={"frequency": "hourly"})
    prefs = engagement.get_or_create_preferences(db_session, user_id=user.id)
    assert prefs.frequency == "immediate"  # unchanged default
