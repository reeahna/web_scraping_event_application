"""Phase 13: engagement endpoints (save/follow/preferences/unsubscribe)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.permissions import REGISTERED_USER
from app.services import engagement

TOMORROW = datetime.now(UTC).date() + timedelta(days=1)


def _visible_event(make_city, make_website, make_event, **over):
    city = make_city()
    website = make_website(city, is_active=True, approved_pattern={"pattern_name": "static_html"})
    event = make_event(city, website=website, start_date=TOMORROW, **over)
    return city, website, event


def _signin(client, make_user, login, email="reg@example.com") -> str:
    make_user(email=email, password="password12345", role_name=REGISTERED_USER)
    login(email, "password12345")
    return client.cookies.get("csrf_token")


def test_save_requires_login(client, make_city, make_website, make_event):
    _, _, event = _visible_event(make_city, make_website, make_event, title="E")
    resp = client.post(f"/account/save/{event.id}", data={"csrf_token": "x"},
                       follow_redirects=False)
    assert resp.status_code in (302, 303, 401, 403)


def test_save_and_appears_on_saved_page(
    client, make_user, login, make_city, make_website, make_event
):
    _, _, event = _visible_event(make_city, make_website, make_event, title="Savable")
    csrf = _signin(client, make_user, login)
    resp = client.post(f"/account/save/{event.id}", data={"csrf_token": csrf},
                       follow_redirects=False)
    assert resp.status_code == 303
    page = client.get("/account/saved")
    assert "Savable" in page.text


def test_saved_page_is_private(
    client, make_user, login, make_city, make_website, make_event, db_session
):
    _, _, event = _visible_event(make_city, make_website, make_event, title="Private One")
    user_a = make_user(email="a@example.com", password="password12345", role_name=REGISTERED_USER)
    engagement.save_event(db_session, user_id=user_a.id, event_id=event.id)
    _signin(client, make_user, login, email="b@example.com")
    page = client.get("/account/saved")
    assert "Private One" not in page.text


def test_follow_city_endpoint(client, make_user, login, make_city, db_session):
    city = make_city()
    csrf = _signin(client, make_user, login)
    resp = client.post(
        "/account/follow",
        data={"csrf_token": csrf, "follow_type": "city", "target_id": city.id, "action": "follow"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    followers = engagement.followers_of(db_session, follow_type="city", target_id=city.id)
    assert len(followers) == 1


def test_alert_preferences_update(client, make_user, login):
    csrf = _signin(client, make_user, login)
    resp = client.post(
        "/account/alerts",
        data={"csrf_token": csrf, "frequency": "daily", "email_enabled": "1",
              "in_app_enabled": "1", "notify_new_events": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    page = client.get("/account/alerts")
    assert "Daily digest" in page.text


def test_unsubscribe_flow(client, make_user, db_session):
    user = make_user(email="u@example.com", password="password12345", role_name=REGISTERED_USER)
    prefs = engagement.get_or_create_preferences(db_session, user_id=user.id)
    prefs.email_enabled = True
    db_session.commit()
    token = prefs.unsubscribe_token

    page = client.get(f"/alerts/unsubscribe?token={token}")
    assert page.status_code == 200
    csrf = client.cookies.get("csrf_token")
    resp = client.post(
        "/alerts/unsubscribe", data={"csrf_token": csrf, "token": token}, follow_redirects=False
    )
    assert resp.status_code == 303
    db_session.refresh(prefs)
    assert prefs.email_enabled is False


def test_save_csrf_required(client, make_user, login, make_city, make_website, make_event):
    _, _, event = _visible_event(make_city, make_website, make_event, title="E")
    _signin(client, make_user, login)
    resp = client.post(f"/account/save/{event.id}", data={"csrf_token": "wrong"},
                       follow_redirects=False)
    assert resp.status_code in (400, 403)


def test_saved_page_greys_out_past_events(
    client, make_user, login, make_city, make_website, make_event, db_session
):
    city, website, upcoming = _visible_event(make_city, make_website, make_event, title="Soon")
    past = make_event(
        city, website=website, title="Already Over",
        start_date=TOMORROW - timedelta(days=10),
    )
    user = make_user(email="p@example.com", password="password12345",
                     role_name=REGISTERED_USER)
    engagement.save_event(db_session, user_id=user.id, event_id=upcoming.id)
    engagement.save_event(db_session, user_id=user.id, event_id=past.id)
    login("p@example.com", "password12345")
    page = client.get("/account/saved").text
    assert "Coming up" in page
    assert f'href="/events/{upcoming.id}"' in page
    # The past event is listed, greyed out, explained, and not linked to a page that 404s.
    assert "Already Over" in page
    assert "saved-item-finished" in page
    assert "This event has already happened" in page
    assert f'href="/events/{past.id}"' not in page
    assert "Remove past events" in page


def test_saved_page_marks_unlisted_future_event(
    client, make_user, login, make_city, make_website, make_event, db_session
):
    _, _, event = _visible_event(make_city, make_website, make_event, title="Pulled")
    user = make_user(email="u@example.com", password="password12345",
                     role_name=REGISTERED_USER)
    engagement.save_event(db_session, user_id=user.id, event_id=event.id)
    event.is_active = False
    db_session.commit()
    login("u@example.com", "password12345")
    page = client.get("/account/saved").text
    assert "No longer listed" in page
    assert f'href="/events/{event.id}"' not in page


def test_clear_past_saved_events(
    client, make_user, login, make_city, make_website, make_event, db_session
):
    city, website, upcoming = _visible_event(make_city, make_website, make_event, title="Keep")
    past = make_event(
        city, website=website, title="Drop", start_date=TOMORROW - timedelta(days=5)
    )
    user = make_user(email="c@example.com", password="password12345",
                     role_name=REGISTERED_USER)
    engagement.save_event(db_session, user_id=user.id, event_id=upcoming.id)
    engagement.save_event(db_session, user_id=user.id, event_id=past.id)
    login("c@example.com", "password12345")
    csrf = client.cookies.get("csrf_token")
    resp = client.post("/account/saved/clear-past", data={"csrf_token": csrf},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert engagement.saved_event_ids(db_session, user_id=user.id) == {upcoming.id}


def test_following_something_that_does_not_exist_is_refused(
    client, make_user, login, make_city, db_session
):
    hidden = make_city(name="Hidden", slug="hidden", is_active=False)
    csrf = _signin(client, make_user, login)
    for follow_type, target_id in (("city", 999999), ("city", hidden.id),
                                   ("category", -5), ("source", 0)):
        resp = client.post(
            "/account/follow",
            data={"csrf_token": csrf, "follow_type": follow_type, "target_id": target_id},
            follow_redirects=False,
        )
        assert resp.status_code == 404, (follow_type, target_id)
        assert engagement.followers_of(
            db_session, follow_type=follow_type, target_id=target_id
        ) == []
