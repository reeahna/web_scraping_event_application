from app.core.permissions import EDITOR, REGISTERED_USER
from app.models.audit_log import AuditLog
from app.models.website import Website


def _csrf(client) -> str:
    return client.cookies.get("csrf_token")


def test_create_website_starts_as_inactive_draft(
    client, make_super_admin, make_city, login, db_session
):
    make_super_admin(email="root@example.com", password="root-pass-1234")
    city = make_city(name="Bloomington Area, IN", slug="bloomington-in")
    login("root@example.com", "root-pass-1234")

    client.get("/admin/websites/new")
    resp = client.post(
        "/admin/websites",
        data={
            "name": "IU Events Calendar",
            "base_url": "https://events.iu.edu",
            "city_id": str(city.id),
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    website = db_session.query(Website).filter(Website.name == "IU Events Calendar").one()
    assert website.is_active is False
    assert website.onboarding_status == "draft"
    assert website.city_id == city.id

    entries = db_session.query(AuditLog).filter(AuditLog.action == "website_created").all()
    assert len(entries) == 1
    assert entries[0].entity_id == website.id


def test_website_assigned_to_correct_city(client, make_super_admin, make_city, login, db_session):
    make_super_admin(email="root2@example.com", password="root-pass-1234")
    city_a = make_city(name="City A", slug="city-a")
    city_b = make_city(name="City B", slug="city-b")
    login("root2@example.com", "root-pass-1234")

    client.get("/admin/websites/new")
    client.post(
        "/admin/websites",
        data={
            "name": "Site For B",
            "base_url": "https://example.com/events",
            "city_id": str(city_b.id),
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    website = db_session.query(Website).filter(Website.name == "Site For B").one()
    assert website.city_id == city_b.id
    assert website.city_id != city_a.id


def test_valid_status_transition_path_to_active(
    client, make_super_admin, make_city, make_website, login, db_session
):
    make_super_admin(email="root3@example.com", password="root-pass-1234")
    city = make_city(name="Transition City", slug="transition-city")
    website = make_website(city, name="Transition Site")
    login("root3@example.com", "root-pass-1234")

    client.get(f"/admin/websites/{website.id}")

    resp = client.post(
        f"/admin/websites/{website.id}/status",
        data={"to_status": "approved", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.refresh(website)
    assert website.onboarding_status == "approved"
    assert website.is_active is False

    resp = client.post(
        f"/admin/websites/{website.id}/status",
        data={"to_status": "active", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.refresh(website)
    assert website.onboarding_status == "active"
    assert website.is_active is True

    actions = {
        e.action
        for e in db_session.query(AuditLog)
        .filter(AuditLog.entity_type == "website", AuditLog.entity_id == website.id)
        .all()
    }
    assert "website_status_changed" in actions


def test_invalid_status_transition_rejected(
    client, make_super_admin, make_city, make_website, login, db_session
):
    make_super_admin(email="root4@example.com", password="root-pass-1234")
    city = make_city(name="Invalidtown", slug="invalidtown")
    website = make_website(city, name="Freshly Created Site")
    login("root4@example.com", "root-pass-1234")

    client.get(f"/admin/websites/{website.id}")
    # draft -> needs_review isn't allowed (only draft -> detecting/approved/archived).
    resp = client.post(
        f"/admin/websites/{website.id}/status",
        data={"to_status": "needs_review", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 409
    db_session.refresh(website)
    assert website.onboarding_status == "draft"


def test_activation_requires_approved_state_first(
    client, make_super_admin, make_city, make_website, login, db_session
):
    make_super_admin(email="root5@example.com", password="root-pass-1234")
    city = make_city(name="Skipsville", slug="skipsville")
    website = make_website(city, name="Skip Site")
    login("root5@example.com", "root-pass-1234")

    client.get(f"/admin/websites/{website.id}")
    # draft -> active directly is not allowed; must go through approved first.
    resp = client.post(
        f"/admin/websites/{website.id}/status",
        data={"to_status": "active", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 409
    db_session.refresh(website)
    assert website.is_active is False


def test_ssrf_unsafe_url_rejected(client, make_super_admin, make_city, login, db_session):
    make_super_admin(email="root6@example.com", password="root-pass-1234")
    city = make_city(name="SSRF City", slug="ssrf-city")
    login("root6@example.com", "root-pass-1234")

    client.get("/admin/websites/new")
    resp = client.post(
        "/admin/websites",
        data={
            "name": "Malicious Site",
            "base_url": "http://169.254.169.254/latest/meta-data/",
            "city_id": str(city.id),
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 422
    assert db_session.query(Website).filter(Website.name == "Malicious Site").first() is None


def test_ssrf_localhost_url_rejected(client, make_super_admin, login):
    make_super_admin(email="root7@example.com", password="root-pass-1234")
    login("root7@example.com", "root-pass-1234")

    client.get("/admin/websites/new")
    resp = client.post(
        "/admin/websites",
        data={
            "name": "Localhost Site",
            "base_url": "http://localhost:8000/events",
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 422


def test_editor_cannot_delete_website(client, make_user, make_city, make_website, login):
    make_user(email="editor@example.com", password="pw-editor12345", role_name=EDITOR)
    city = make_city(name="Editor City", slug="editor-city")
    website = make_website(city, name="Editor's Site")
    login("editor@example.com", "pw-editor12345")

    resp = client.get(f"/admin/websites/{website.id}/delete")
    assert resp.status_code == 403


def test_registered_user_cannot_create_website(client, make_user, login):
    make_user(
        email="registered@example.com",
        password="pw-registered12345",
        role_name=REGISTERED_USER,
    )
    login("registered@example.com", "pw-registered12345")

    resp = client.get("/admin/websites/new")
    assert resp.status_code == 403


def test_unauthorized_access_to_website_list(client):
    resp = client.get("/admin/websites", headers={"accept": "application/json"})
    assert resp.status_code == 401


def test_archive_website(client, make_super_admin, make_city, make_website, login, db_session):
    make_super_admin(email="root8@example.com", password="root-pass-1234")
    city = make_city(name="Archive City", slug="archive-city")
    website = make_website(city, name="Archive Site")
    login("root8@example.com", "root-pass-1234")

    client.get(f"/admin/websites/{website.id}")
    resp = client.post(
        f"/admin/websites/{website.id}/status",
        data={"to_status": "archived", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.refresh(website)
    assert website.onboarding_status == "archived"
    assert website.archived_at is not None
    assert website.is_active is False

    # Archived is terminal — no further transitions allowed.
    resp = client.post(
        f"/admin/websites/{website.id}/status",
        data={"to_status": "active", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 409


def test_deletion_blocked_when_website_has_unarchived_events(
    client, make_super_admin, make_city, make_website, db_session, login
):
    make_super_admin(email="root10@example.com", password="root-pass-1234")
    city = make_city(name="Really Blocked City", slug="really-blocked-city")
    website = make_website(city, name="Really Blocked Site")

    from app.models.event import Event

    event = Event(
        title="Tied Event",
        canonical_url="https://example.com/tied-event",
        source="Test",
        city_id=city.id,
        website_id=website.id,
    )
    db_session.add(event)
    db_session.commit()

    login("root10@example.com", "root-pass-1234")

    delete_resp = client.post(
        f"/admin/websites/{website.id}/delete",
        data={"confirm_name": website.name, "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert delete_resp.status_code == 409


def test_successful_deletion_after_archiving_dependent_events(
    client, make_super_admin, make_city, make_website, db_session, login
):
    make_super_admin(email="root11@example.com", password="root-pass-1234")
    city = make_city(name="Cleanup City", slug="cleanup-city")
    website = make_website(city, name="Cleanup Site")

    from datetime import UTC, datetime

    from app.models.event import Event

    event = Event(
        title="Archived Event",
        canonical_url="https://example.com/archived-event",
        source="Test",
        city_id=city.id,
        website_id=website.id,
        archived_at=datetime.now(UTC),
    )
    db_session.add(event)
    db_session.commit()

    login("root11@example.com", "root-pass-1234")

    delete_resp = client.post(
        f"/admin/websites/{website.id}/delete",
        data={"confirm_name": website.name, "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert delete_resp.status_code == 303
    assert db_session.query(Website).filter(Website.id == website.id).first() is None


def test_website_deletion_requires_matching_name_confirmation(
    client, make_super_admin, make_city, make_website, login, db_session
):
    make_super_admin(email="root12@example.com", password="root-pass-1234")
    city = make_city(name="Typo City", slug="typo-city-2")
    website = make_website(city, name="Typo Site")
    login("root12@example.com", "root-pass-1234")

    resp = client.post(
        f"/admin/websites/{website.id}/delete",
        data={"confirm_name": "wrong name", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert db_session.query(Website).filter(Website.id == website.id).first() is not None


def test_placeholder_actions_do_not_change_status_but_are_audited(
    client, make_super_admin, make_city, make_website, login, db_session
):
    make_super_admin(email="root13@example.com", password="root-pass-1234")
    city = make_city(name="Placeholder City", slug="placeholder-city")
    website = make_website(city, name="Placeholder Site")
    login("root13@example.com", "root-pass-1234")

    client.get(f"/admin/websites/{website.id}")
    resp = client.post(
        f"/admin/websites/{website.id}/detect-pattern",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.refresh(website)
    assert website.onboarding_status == "draft"

    entries = (
        db_session.query(AuditLog).filter(AuditLog.action == "pattern_detection_requested").all()
    )
    assert len(entries) == 1
    assert entries[0].entity_id == website.id
