from app.core.permissions import REGISTERED_USER
from app.models.audit_log import AuditLog
from app.models.role import Role
from app.models.user_role import UserRole


def test_super_admin_assigns_role_to_user(client, make_super_admin, make_user, login, db_session):
    make_super_admin(email="root@example.com", password="root-pass-1234")
    target = make_user(email="target@example.com", password="pw-target12345")
    login("root@example.com", "root-pass-1234")

    registered_role = db_session.query(Role).filter(Role.name == REGISTERED_USER).one()
    csrf = client.cookies.get("csrf_token")

    resp = client.post(
        f"/admin/users/{target.id}/roles",
        data={"role_id": registered_role.id, "action": "assign", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    link = (
        db_session.query(UserRole)
        .filter(UserRole.user_id == target.id, UserRole.role_id == registered_role.id)
        .first()
    )
    assert link is not None

    entries = db_session.query(AuditLog).filter(AuditLog.action == "role_assigned").all()
    assert len(entries) == 1
    assert entries[0].entity_id == target.id
