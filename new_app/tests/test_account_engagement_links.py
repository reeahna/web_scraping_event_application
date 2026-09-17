"""The account page lists what a user actually has, not a roadmap.

Saved events, follows and alert preferences all shipped, but the page kept
advertising them as "coming soon". Following was the one that genuinely looked
unbuilt: it worked from any event page, and nothing anywhere listed what a user
followed.
"""

import pytest

from app.services import engagement


@pytest.fixture
def account_user(make_user, login, db_session, make_city):
    user = make_user(email="engaged@example.com", password="engaged-pass-123")
    city = make_city(name="Followed City", slug="followed-city")
    db_session.commit()
    login("engaged@example.com", "engaged-pass-123")
    return user, city


def test_the_coming_soon_block_is_gone(client, account_user):
    html = client.get("/account").text
    assert "Coming soon" not in html
    assert "not yet available" not in html


def test_the_shipped_features_are_linked(client, account_user):
    html = client.get("/account").text
    assert 'href="/account/saved"' in html
    assert 'href="/account/alerts"' in html


def test_saved_events_are_counted(client, account_user, make_website, make_event, db_session):
    user, city = account_user
    event = make_event(city, title="Kept", canonical_url="https://example.com/kept")
    engagement.save_event(db_session, user_id=user.id, event_id=event.id)

    assert engagement.saved_event_count(db_session, user_id=user.id) == 1
    html = client.get("/account").text
    condensed = " ".join(html.split())
    assert "<strong>1</strong> saved event" in condensed
    # Singular for one, so the count is clearly being read rather than guessed.
    assert "saved events" not in html


def test_followed_cities_are_listed_with_a_way_to_unfollow(
    client, account_user, db_session
):
    """Nothing in the app listed a user's follows before this."""
    user, city = account_user
    engagement.follow(db_session, user_id=user.id, follow_type="city", target_id=city.id)

    html = client.get("/account").text
    assert "Followed City" in html
    assert 'value="unfollow"' in html


def test_following_nothing_explains_where_to_start(client, account_user):
    html = client.get("/account").text
    assert "not following any cities yet" in html


def test_unfollow_from_the_account_page_returns_there(client, account_user, db_session):
    user, city = account_user
    engagement.follow(db_session, user_id=user.id, follow_type="city", target_id=city.id)

    response = client.post(
        "/account/follow",
        data={
            "follow_type": "city",
            "target_id": city.id,
            "action": "unfollow",
            "next": "/account",
            "csrf_token": client.cookies.get("csrf_token"),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/account"
    assert engagement.followed_cities(db_session, user_id=user.id) == []


def test_follows_are_scoped_to_the_acting_user(db_session, make_user, make_city):
    """A follow stores only a type and a target id, so the listing join must
    filter by user as well."""
    mine = make_user(email="mine@example.com", password="mine-pass-1234")
    theirs = make_user(email="theirs@example.com", password="theirs-pass-123")
    city = make_city(name="Shared City", slug="shared-city")
    db_session.commit()
    engagement.follow(db_session, user_id=theirs.id, follow_type="city", target_id=city.id)

    assert engagement.followed_cities(db_session, user_id=mine.id) == []
    assert [c.id for c in engagement.followed_cities(db_session, user_id=theirs.id)] == [city.id]
