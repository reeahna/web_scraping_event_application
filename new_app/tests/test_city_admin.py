from app.core.permissions import EDITOR
from app.models.audit_log import AuditLog
from app.models.city import City


def _csrf(client) -> str:
    return client.cookies.get("csrf_token")


def test_create_city(client, make_super_admin, login, db_session):
    make_super_admin(email="root@example.com", password="root-pass-1234")
    login("root@example.com", "root-pass-1234")

    client.get("/admin/cities/new")
    resp = client.post(
        "/admin/cities",
        data={
            "name": "Bloomington Area, IN",
            "slug": "bloomington-in",
            "timezone": "America/Indiana/Indianapolis",
            "is_active": "on",
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    city = db_session.query(City).filter(City.slug == "bloomington-in").one()
    assert city.name == "Bloomington Area, IN"
    assert city.is_active is True

    entries = db_session.query(AuditLog).filter(AuditLog.action == "city_created").all()
    assert len(entries) == 1
    assert entries[0].entity_id == city.id


def test_duplicate_slug_rejected(client, make_super_admin, make_city, login):
    make_super_admin(email="root2@example.com", password="root-pass-1234")
    make_city(name="Existing City", slug="existing-city")
    login("root2@example.com", "root-pass-1234")

    client.get("/admin/cities/new")
    resp = client.post(
        "/admin/cities",
        data={
            "name": "Another City",
            "slug": "existing-city",
            "timezone": "UTC",
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 409
    assert "already exists" in resp.text


def test_invalid_timezone_rejected(client, make_super_admin, login, db_session):
    make_super_admin(email="root3@example.com", password="root-pass-1234")
    login("root3@example.com", "root-pass-1234")

    client.get("/admin/cities/new")
    resp = client.post(
        "/admin/cities",
        data={
            "name": "Bad TZ City",
            "slug": "bad-tz-city",
            "timezone": "Not/ARealZone",
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 422
    assert db_session.query(City).filter(City.slug == "bad-tz-city").first() is None


def test_invalid_slug_rejected(client, make_super_admin, login):
    make_super_admin(email="root4@example.com", password="root-pass-1234")
    login("root4@example.com", "root-pass-1234")

    client.get("/admin/cities/new")
    resp = client.post(
        "/admin/cities",
        data={
            "name": "Bad Slug City",
            "slug": "Not A Valid Slug!",
            "timezone": "UTC",
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 422


def test_activate_and_deactivate_city(client, make_super_admin, make_city, login, db_session):
    make_super_admin(email="root5@example.com", password="root-pass-1234")
    city = make_city(name="Togglesville", slug="togglesville", is_active=True)
    login("root5@example.com", "root-pass-1234")

    client.get(f"/admin/cities/{city.id}")
    resp = client.post(
        f"/admin/cities/{city.id}/deactivate",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.refresh(city)
    assert city.is_active is False

    resp = client.post(
        f"/admin/cities/{city.id}/activate",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.refresh(city)
    assert city.is_active is True

    actions = {
        e.action
        for e in db_session.query(AuditLog)
        .filter(AuditLog.entity_type == "city", AuditLog.entity_id == city.id)
        .all()
    }
    assert "city_deactivated" in actions
    assert "city_activated" in actions


def test_editor_cannot_delete_city(client, make_user, make_city, login):
    make_user(email="editor@example.com", password="pw-editor12345", role_name=EDITOR)
    city = make_city(name="Protected City", slug="protected-city")
    login("editor@example.com", "pw-editor12345")

    resp = client.get(f"/admin/cities/{city.id}/delete")
    assert resp.status_code == 403


def test_unauthorized_access_to_city_list(client):
    resp = client.get("/admin/cities", headers={"accept": "application/json"})
    assert resp.status_code == 401


def test_unauthenticated_browser_request_redirects_to_login(client):
    resp = client.get("/admin/cities", headers={"accept": "text/html"}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/auth/login")


def test_deletion_blocked_by_unarchived_events(
    client, make_super_admin, make_city, make_event, login
):
    make_super_admin(email="root6@example.com", password="root-pass-1234")
    city = make_city(name="Eventful City", slug="eventful-city")
    make_event(city, title="Some Event")
    login("root6@example.com", "root-pass-1234")

    impact_resp = client.get(f"/admin/cities/{city.id}/delete")
    assert impact_resp.status_code == 200
    assert "cannot be permanently deleted" in impact_resp.text

    delete_resp = client.post(
        f"/admin/cities/{city.id}/delete",
        data={"confirm_slug": city.slug, "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert delete_resp.status_code == 409


def test_deletion_blocked_by_unarchived_websites(
    client, make_super_admin, make_city, make_website, login
):
    make_super_admin(email="root7@example.com", password="root-pass-1234")
    city = make_city(name="Websitey City", slug="websitey-city")
    make_website(city, name="Some Site")
    login("root7@example.com", "root-pass-1234")

    delete_resp = client.post(
        f"/admin/cities/{city.id}/delete",
        data={"confirm_slug": city.slug, "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert delete_resp.status_code == 409


def test_successful_deletion_after_archiving_and_removing_dependencies(
    client, make_super_admin, make_city, make_event, make_website, login, db_session
):
    make_super_admin(email="root8@example.com", password="root-pass-1234")
    city = make_city(name="Cleanable City", slug="cleanable-city")
    make_event(city, title="Old Event")
    make_website(city, name="Old Site")
    login("root8@example.com", "root-pass-1234")

    client.get(f"/admin/cities/{city.id}/delete")

    resp = client.post(
        f"/admin/cities/{city.id}/archive-events",
        data={"confirm_slug": city.slug, "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    resp = client.post(
        f"/admin/cities/{city.id}/delete-events",
        data={"confirm_slug": city.slug, "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    resp = client.post(
        f"/admin/cities/{city.id}/archive-websites",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    impact_resp = client.get(f"/admin/cities/{city.id}/delete")
    assert "eligible for permanent deletion" in impact_resp.text

    delete_resp = client.post(
        f"/admin/cities/{city.id}/delete",
        data={"confirm_slug": city.slug, "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert delete_resp.status_code == 303
    assert db_session.query(City).filter(City.id == city.id).first() is None

    entries = db_session.query(AuditLog).filter(AuditLog.action == "city_deleted").all()
    assert len(entries) == 1
    assert entries[0].entity_id == city.id


def test_deletion_requires_matching_slug_confirmation(
    client, make_super_admin, make_city, login, db_session
):
    make_super_admin(email="root9@example.com", password="root-pass-1234")
    city = make_city(name="Typo City", slug="typo-city")
    login("root9@example.com", "root-pass-1234")

    client.get(f"/admin/cities/{city.id}/delete")
    resp = client.post(
        f"/admin/cities/{city.id}/delete",
        data={"confirm_slug": "wrong-slug", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert db_session.query(City).filter(City.id == city.id).first() is not None
