from app.core.permissions import SUPER_ADMINISTRATOR
from app.models.role import Role
from app.models.user_role import UserRole


def test_cannot_deactivate_last_super_admin(client, make_super_admin, login, db_session):
    admin = make_super_admin(email="onlyadmin@example.com", password="pw-admin12345")
    login("onlyadmin@example.com", "pw-admin12345")

    csrf = client.cookies.get("csrf_token")
    resp = client.post(
        f"/admin/users/{admin.id}/deactivate",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 403

    db_session.refresh(admin)
    assert admin.is_active is True


def test_cannot_revoke_last_super_admin_role(client, make_super_admin, login, db_session):
    admin = make_super_admin(email="onlyadmin2@example.com", password="pw-admin12345")
    login("onlyadmin2@example.com", "pw-admin12345")

    role = db_session.query(Role).filter(Role.name == SUPER_ADMINISTRATOR).one()
    csrf = client.cookies.get("csrf_token")

    resp = client.post(
        f"/admin/users/{admin.id}/roles",
        data={"role_id": role.id, "action": "revoke", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 403

    link = (
        db_session.query(UserRole)
        .filter(UserRole.user_id == admin.id, UserRole.role_id == role.id)
        .first()
    )
    assert link is not None


def test_can_revoke_super_admin_role_when_another_remains(
    client, make_super_admin, login, db_session
):
    make_super_admin(email="admin1@example.com", password="pw-admin12345")
    admin2 = make_super_admin(email="admin2@example.com", password="pw-admin22345")
    login("admin1@example.com", "pw-admin12345")

    role = db_session.query(Role).filter(Role.name == SUPER_ADMINISTRATOR).one()
    csrf = client.cookies.get("csrf_token")

    resp = client.post(
        f"/admin/users/{admin2.id}/roles",
        data={"role_id": role.id, "action": "revoke", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    link = (
        db_session.query(UserRole)
        .filter(UserRole.user_id == admin2.id, UserRole.role_id == role.id)
        .first()
    )
    assert link is None
