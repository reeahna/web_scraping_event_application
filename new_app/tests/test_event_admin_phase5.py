import json

import pytest

from app.core.permissions import ADMINISTRATOR, EDITOR, REGISTERED_USER
from app.models.audit_log import AuditLog
from app.models.event import Event


def _csrf(client) -> str:
    return client.cookies.get("csrf_token")


def _admin(make_user, login, *, role=ADMINISTRATOR, email="event-admin@example.com"):
    user = make_user(email=email, password="event-pass-123", role_name=role)
    login(email, "event-pass-123")
    return user


def test_registered_user_denied_event_admin_pages(client, make_user, login):
    _admin(
        make_user,
        login,
        role=REGISTERED_USER,
        email="registered-events@example.com",
    )
    assert client.get("/admin/events").status_code == 403


def test_editor_can_view_and_review_but_cannot_archive_or_delete(
    client, make_user, make_city, make_event, login
):
    _admin(make_user, login, role=EDITOR, email="event-editor@example.com")
    event = make_event(make_city())

    detail = client.get(f"/admin/events/{event.id}")
    assert detail.status_code == 200
    assert f'action="/admin/events/{event.id}/review-status"' in detail.text
    assert f'action="/admin/events/{event.id}/delete"' not in detail.text

    archive = client.post(
        f"/admin/events/{event.id}/lifecycle",
        data={"action": "archive", "csrf_token": _csrf(client)},
    )
    assert archive.status_code == 403


def test_backend_rejects_unauthorized_direct_event_post(
    client, make_user, make_city, make_event, login
):
    _admin(make_user, login, role=REGISTERED_USER, email="direct-post@example.com")
    event = make_event(make_city())
    response = client.post(
        f"/admin/events/{event.id}/lifecycle",
        data={"action": "deactivate", "csrf_token": _csrf(client)},
    )
    assert response.status_code == 403
    duplicate_response = client.post(
        f"/admin/events/{event.id}/duplicate-status",
        data={
            "duplicate_status": "confirmed_duplicate",
            "preferred_event_id": "",
            "csrf_token": _csrf(client),
        },
    )
    assert duplicate_response.status_code == 403


def test_event_list_search_and_filters(
    client, make_user, make_city, make_website, make_category, make_event, login
):
    _admin(make_user, login)
    city_a = make_city(name="Alpha City", slug="alpha-city")
    city_b = make_city(name="Beta City", slug="beta-city")
    website = make_website(city_a, name="Special Source")
    category = make_category(name="Special Category", slug="special-category")
    matching = make_event(
        city_a,
        title="Unique Concert",
        canonical_url="https://example.com/unique",
        website=website,
        category=category,
        is_active=False,
        review_status="reviewed",
        duplicate_status="possible_duplicate",
    )
    make_event(city_b, title="Different Event", canonical_url="https://example.com/different")

    paths = (
        "/admin/events?q=Unique",
        f"/admin/events?city_id={city_a.id}",
        f"/admin/events?website_id={website.id}",
        f"/admin/events?category_id={category.id}",
        "/admin/events?active=no",
        "/admin/events?archived=no",
        "/admin/events?review_status=reviewed",
        "/admin/events?duplicate_status=possible_duplicate",
    )
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200
        assert matching.title in response.text
        assert "Different Event" not in response.text or path.endswith("archived=no")


def test_event_list_archived_filter(client, make_user, make_city, make_event, login):
    _admin(make_user, login)
    city = make_city()
    archived = make_event(city, title="Archived One", archived=True)
    make_event(city, title="Current One", canonical_url="https://example.com/current")

    response = client.get("/admin/events?archived=yes")
    assert archived.title in response.text
    assert "Current One" not in response.text


def test_event_list_pagination(client, make_user, make_city, make_event, login):
    _admin(make_user, login)
    city = make_city()
    for index in range(21):
        make_event(
            city,
            title=f"Paged Event {index:02d}",
            canonical_url=f"https://example.com/paged/{index}",
        )

    first = client.get("/admin/events")
    second = client.get("/admin/events?page=2")
    assert 'href="/admin/events?page=2">Next</a>' in first.text
    assert "Paged Event" in second.text


def test_event_detail_source_fields_are_read_only(client, make_user, make_city, make_event, login):
    _admin(make_user, login)
    event = make_event(
        make_city(),
        title="Immutable Source Title",
        description="Immutable source description",
        external_source_id="source-123",
        image_url="https://example.com/image.jpg",
    )
    response = client.get(f"/admin/events/{event.id}")
    assert response.status_code == 200
    assert "Authoritative extracted fields are read-only" in response.text
    for field in (
        "title",
        "description",
        "canonical_url",
        "website_id",
        "start_date",
        "start_time",
        "image_url",
        "external_source_id",
    ):
        assert f'name="{field}"' not in response.text


@pytest.mark.parametrize(
    ("initial_active", "action", "expected_active", "audit_action"),
    [
        (False, "activate", True, "event_activated"),
        (True, "deactivate", False, "event_deactivated"),
    ],
)
def test_event_activation_and_deactivation(
    client,
    make_user,
    make_city,
    make_event,
    login,
    db_session,
    initial_active,
    action,
    expected_active,
    audit_action,
):
    _admin(make_user, login)
    event = make_event(make_city(), is_active=initial_active)
    response = client.post(
        f"/admin/events/{event.id}/lifecycle",
        data={"action": action, "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    db_session.refresh(event)
    assert event.is_active is expected_active
    assert db_session.query(AuditLog).filter(AuditLog.action == audit_action).count() == 1


def test_event_archive_and_restore(client, make_user, make_city, make_event, login, db_session):
    _admin(make_user, login)
    event = make_event(make_city(), is_active=True)
    archive = client.post(
        f"/admin/events/{event.id}/lifecycle",
        data={"action": "archive", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert archive.status_code == 303
    db_session.refresh(event)
    assert event.archived_at is not None and event.is_active is False

    restore = client.post(
        f"/admin/events/{event.id}/lifecycle",
        data={"action": "restore", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert restore.status_code == 303
    db_session.refresh(event)
    assert event.archived_at is None and event.is_active is False
    actions = {entry.action for entry in db_session.query(AuditLog).all()}
    assert {"event_archived", "event_restored"}.issubset(actions)


def test_invalid_lifecycle_transition_rejected(client, make_user, make_city, make_event, login):
    _admin(make_user, login)
    event = make_event(make_city(), is_active=True)
    response = client.post(
        f"/admin/events/{event.id}/lifecycle",
        data={"action": "activate", "csrf_token": _csrf(client)},
    )
    assert response.status_code == 409


def test_permanent_deletion_requires_archived_event(
    client, make_user, make_city, make_event, login, db_session
):
    _admin(make_user, login)
    city = make_city()
    current = make_event(city, title="Current Delete Test")
    denied = client.post(
        f"/admin/events/{current.id}/delete",
        data={"confirm_title": current.title, "csrf_token": _csrf(client)},
    )
    assert denied.status_code == 409

    archived = make_event(
        city,
        title="Archived Delete Test",
        canonical_url="https://example.com/delete-archived",
        archived=True,
    )
    deleted_id = archived.id
    allowed = client.post(
        f"/admin/events/{archived.id}/delete",
        data={"confirm_title": archived.title, "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert allowed.status_code == 303
    db_session.expire_all()
    assert db_session.get(Event, deleted_id) is None
    assert (
        db_session.query(AuditLog).filter(AuditLog.action == "event_permanently_deleted").count()
        == 1
    )


def test_category_override_clear_and_reclassification_preserve_override(
    client, make_user, make_city, make_category, make_event, login, db_session
):
    _admin(make_user, login)
    base = make_category(name="Base Category", slug="base-category")
    override = make_category(name="Override Category", slug="override-category")
    event = make_event(make_city(), category=base)

    response = client.post(
        f"/admin/events/{event.id}/category-override",
        data={
            "category_id": override.id,
            "reason": "Curator decision",
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    db_session.refresh(event)
    assert event.effective_category.id == override.id

    client.post(
        f"/admin/events/{event.id}/categorize",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    db_session.refresh(event)
    assert event.effective_category.id == override.id

    cleared = client.post(
        f"/admin/events/{event.id}/category-override/clear",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert cleared.status_code == 303
    db_session.refresh(event)
    assert event.category_override_id is None
    actions = {entry.action for entry in db_session.query(AuditLog).all()}
    assert {"event_category_overridden", "event_category_override_cleared"}.issubset(actions)


def test_inactive_category_cannot_be_assigned(
    client, make_user, make_city, make_category, make_event, login
):
    _admin(make_user, login)
    category = make_category(name="Inactive Category", slug="inactive-category", is_active=False)
    event = make_event(make_city())
    response = client.post(
        f"/admin/events/{event.id}/category-override",
        data={"category_id": category.id, "csrf_token": _csrf(client)},
    )
    assert response.status_code == 422


def test_location_correction_preserves_source_and_audits_before_after(
    client, make_user, make_city, make_event, login, db_session
):
    _admin(make_user, login)
    event = make_event(make_city(), venue="Source Venue", address="Source Address")
    response = client.post(
        f"/admin/events/{event.id}/location-correction",
        data={
            "venue": "Corrected Venue",
            "address": "Corrected Address",
            "latitude": "39.17",
            "longitude": "-86.52",
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    db_session.refresh(event)
    assert event.venue == "Source Venue"
    assert event.address == "Source Address"
    assert event.public_venue == "Corrected Venue"
    entry = db_session.query(AuditLog).filter(AuditLog.action == "event_location_corrected").one()
    assert json.loads(entry.before_state)["venue"] is None
    assert json.loads(entry.after_state)["venue"] == "Corrected Venue"


@pytest.mark.parametrize(
    ("latitude", "longitude"), [("91", "0"), ("0", "181"), ("not-a-number", "0")]
)
def test_invalid_location_coordinates_rejected(
    client, make_user, make_city, make_event, login, latitude, longitude
):
    _admin(make_user, login)
    event = make_event(make_city())
    response = client.post(
        f"/admin/events/{event.id}/location-correction",
        data={
            "venue": "",
            "address": "",
            "latitude": latitude,
            "longitude": longitude,
            "csrf_token": _csrf(client),
        },
    )
    assert response.status_code == 422


def test_location_correction_rejects_unexpected_fields(
    client, make_user, make_city, make_event, login
):
    _admin(make_user, login)
    event = make_event(make_city())
    response = client.post(
        f"/admin/events/{event.id}/location-correction",
        data={
            "venue": "Allowed",
            "address": "",
            "latitude": "",
            "longitude": "",
            "title": "Forbidden",
            "csrf_token": _csrf(client),
        },
    )
    assert response.status_code == 422


def test_duplicate_status_update_is_persisted_audited_and_does_not_merge(
    client, make_user, make_city, make_event, login, db_session
):
    _admin(make_user, login)
    city = make_city()
    preferred = make_event(city, title="Preferred")
    duplicate = make_event(
        city,
        title="Duplicate",
        canonical_url="https://example.com/duplicate-record",
    )
    response = client.post(
        f"/admin/events/{duplicate.id}/duplicate-status",
        data={
            "duplicate_status": "confirmed_duplicate",
            "preferred_event_id": preferred.id,
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    db_session.refresh(duplicate)
    assert duplicate.duplicate_status == "confirmed_duplicate"
    assert duplicate.duplicate_preferred_event_id == preferred.id
    assert db_session.get(Event, preferred.id) is not None
    assert db_session.get(Event, duplicate.id) is not None
    assert (
        db_session.query(AuditLog).filter(AuditLog.action == "duplicate_resolution_changed").count()
        == 1
    )
