import pytest

from app.core.permissions import ADMINISTRATOR, REGISTERED_USER, SUPER_ADMINISTRATOR
from app.models.audit_log import AuditLog
from app.models.permission import Permission
from app.models.role import Role
from app.models.role_permission import RolePermission
from app.models.user_role import UserRole


def _csrf(client) -> str:
    return client.cookies.get("csrf_token")


def _make_role_manager(db_session, make_user):
    role = Role(name="Role Manager", description="Can manage ordinary roles")
    db_session.add(role)
    db_session.flush()
    permission = db_session.query(Permission).filter(Permission.code == "roles.manage").one()
    db_session.add(RolePermission(role_id=role.id, permission_id=permission.id))
    db_session.commit()
    return make_user(
        email="role-manager@example.com",
        password="manager-pass-123",
        role_name=role.name,
    )


def test_registered_user_cannot_assign_roles(client, make_user, login, db_session):
    actor = make_user(
        email="no-role-access@example.com",
        password="registered-pass-123",
        role_name=REGISTERED_USER,
    )
    target = make_user(email="role-target@example.com", password="target-pass-123")
    role = db_session.query(Role).filter(Role.name == REGISTERED_USER).one()
    login(actor.email, "registered-pass-123")

    response = client.post(
        f"/admin/users/{target.id}/roles",
        data={"role_id": role.id, "action": "assign", "csrf_token": _csrf(client)},
    )
    assert response.status_code == 403


@pytest.mark.parametrize("elevated_role", [ADMINISTRATOR, SUPER_ADMINISTRATOR])
def test_non_super_role_manager_cannot_assign_elevated_roles(
    client, make_user, login, db_session, elevated_role
):
    actor = _make_role_manager(db_session, make_user)
    target = make_user(email="elevation-target@example.com", password="target-pass-123")
    role = db_session.query(Role).filter(Role.name == elevated_role).one()
    login(actor.email, "manager-pass-123")

    response = client.post(
        f"/admin/users/{target.id}/roles",
        data={"role_id": role.id, "action": "assign", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert (
        db_session.query(UserRole)
        .filter(UserRole.user_id == target.id, UserRole.role_id == role.id)
        .first()
        is None
    )


@pytest.mark.parametrize("elevated_role", [ADMINISTRATOR, SUPER_ADMINISTRATOR])
def test_non_super_role_manager_cannot_revoke_elevated_roles(
    client, make_user, make_super_admin, login, db_session, elevated_role
):
    # A second super admin exists so the last-active-super-admin safeguard can't
    # be the reason revocation is blocked here — isolates the can_assign_role check.
    make_super_admin(email="other-root@example.com", password="other-root-pass-1")
    actor = _make_role_manager(db_session, make_user)
    target = make_super_admin(email="revoke-target-root@example.com", password="target-pass-123")
    role = db_session.query(Role).filter(Role.name == elevated_role).one()
    if elevated_role == ADMINISTRATOR:
        db_session.add(UserRole(user_id=target.id, role_id=role.id))
        db_session.commit()

    login(actor.email, "manager-pass-123")

    response = client.post(
        f"/admin/users/{target.id}/roles",
        data={"role_id": role.id, "action": "revoke", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert (
        db_session.query(UserRole)
        .filter(UserRole.user_id == target.id, UserRole.role_id == role.id)
        .first()
        is not None
    )


def test_cannot_assign_deactivated_role(client, make_super_admin, make_user, login, db_session):
    actor = make_super_admin(email="deactivator-root@example.com", password="root-pass-123")
    target = make_user(email="deactivated-role-target@example.com", password="target-pass-123")
    role = Role(name="Retired Role", description="no longer offered", is_active=False)
    db_session.add(role)
    db_session.commit()
    login(actor.email, "root-pass-123")

    response = client.post(
        f"/admin/users/{target.id}/roles",
        data={"role_id": role.id, "action": "assign", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert response.status_code == 409
    assert (
        db_session.query(UserRole)
        .filter(UserRole.user_id == target.id, UserRole.role_id == role.id)
        .first()
        is None
    )


@pytest.mark.parametrize("elevated_role", [ADMINISTRATOR, SUPER_ADMINISTRATOR])
def test_super_admin_can_assign_elevated_roles_and_assignment_is_audited(
    client, make_super_admin, make_user, login, db_session, elevated_role
):
    actor = make_super_admin(email="elevating-root@example.com", password="root-pass-123")
    target = make_user(email="elevated-target@example.com", password="target-pass-123")
    role = db_session.query(Role).filter(Role.name == elevated_role).one()
    login(actor.email, "root-pass-123")

    response = client.post(
        f"/admin/users/{target.id}/roles",
        data={"role_id": role.id, "action": "assign", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    entry = db_session.query(AuditLog).filter(AuditLog.action == "role_assigned").one()
    assert entry.user_id == actor.id
    assert entry.entity_id == target.id
    assert elevated_role in (entry.after_state or "")


def test_role_removal_is_audited(client, make_super_admin, make_user, login, db_session):
    actor = make_super_admin(email="revoking-root@example.com", password="root-pass-123")
    target = make_user(
        email="revoke-target@example.com",
        password="target-pass-123",
        role_name=REGISTERED_USER,
    )
    role = db_session.query(Role).filter(Role.name == REGISTERED_USER).one()
    login(actor.email, "root-pass-123")

    response = client.post(
        f"/admin/users/{target.id}/roles",
        data={"role_id": role.id, "action": "revoke", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    entry = db_session.query(AuditLog).filter(AuditLog.action == "role_unassigned").one()
    assert entry.user_id == actor.id
    assert entry.entity_id == target.id


def test_account_route_does_not_accept_self_role_change(client, make_user, login, db_session):
    user = make_user(
        email="self-change@example.com",
        password="registered-pass-123",
        role_name=REGISTERED_USER,
    )
    elevated = db_session.query(Role).filter(Role.name == ADMINISTRATOR).one()
    login(user.email, "registered-pass-123")

    response = client.post(
        "/account",
        data={
            "display_name": "Self Change",
            "role_id": elevated.id,
            "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert {assignment.role.name for assignment in user.user_roles} == {REGISTERED_USER}
