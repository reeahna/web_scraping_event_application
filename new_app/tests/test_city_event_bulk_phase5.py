import pytest

from app.models.audit_log import AuditLog
from app.services.cities import archive_city_events


def _csrf(client) -> str:
    return client.cookies.get("csrf_token")


def test_bulk_city_event_archival_requires_confirmation(
    client, make_super_admin, make_city, make_event, login
):
    make_super_admin(email="bulk-confirm@example.com", password="bulk-pass-123")
    city = make_city()
    make_event(city)
    login("bulk-confirm@example.com", "bulk-pass-123")
    response = client.post(
        f"/admin/cities/{city.id}/archive-events",
        data={"csrf_token": _csrf(client)},
    )
    assert response.status_code == 422


def test_bulk_city_event_archival_deactivates_and_audits_count(
    client, make_super_admin, make_city, make_event, login, db_session
):
    make_super_admin(email="bulk-archive@example.com", password="bulk-pass-123")
    city = make_city()
    first = make_event(city, title="Bulk One")
    second = make_event(city, title="Bulk Two", canonical_url="https://example.com/bulk-two")
    login("bulk-archive@example.com", "bulk-pass-123")
    response = client.post(
        f"/admin/cities/{city.id}/archive-events",
        data={"confirm_slug": city.slug, "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    db_session.refresh(first)
    db_session.refresh(second)
    assert first.archived_at is not None and first.is_active is False
    assert second.archived_at is not None and second.is_active is False
    audit = db_session.query(AuditLog).filter(AuditLog.action == "bulk_city_events_archived").one()
    assert '"archived_count": 2' in audit.after_state


def test_bulk_permanent_deletion_only_removes_archived_events(
    client, make_super_admin, make_city, make_event, login, db_session
):
    make_super_admin(email="bulk-delete@example.com", password="bulk-pass-123")
    city = make_city()
    current = make_event(city, title="Keep Current")
    archived = make_event(
        city,
        title="Delete Archived",
        canonical_url="https://example.com/delete-bulk",
        archived=True,
    )
    archived_id = archived.id
    login("bulk-delete@example.com", "bulk-pass-123")
    response = client.post(
        f"/admin/cities/{city.id}/delete-events",
        data={"confirm_slug": city.slug, "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    db_session.expire_all()
    assert db_session.get(type(current), current.id) is not None
    assert db_session.get(type(archived), archived_id) is None
    audit = db_session.query(AuditLog).filter(AuditLog.action == "bulk_city_events_deleted").one()
    assert '"deleted_count": 1' in audit.before_state


def test_bulk_city_event_archival_rolls_back_on_commit_failure(
    monkeypatch, db_session, make_city, make_event
):
    city = make_city()
    event = make_event(city)

    def fail_commit():
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(db_session, "commit", fail_commit)
    with pytest.raises(RuntimeError):
        archive_city_events(db_session, city)
    db_session.rollback()
    db_session.refresh(event)
    assert event.archived_at is None
