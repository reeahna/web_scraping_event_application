from app.core.permissions import EDITOR, REGISTERED_USER
from app.models.role import Role
from app.models.user_role import UserRole
from app.services.rbac import get_effective_permissions


def test_effective_permissions_union_multiple_roles(make_user, db_session):
    user = make_user(email="multi@example.com", password="pw-multi12345", role_name=EDITOR)
    registered_role = db_session.query(Role).filter(Role.name == REGISTERED_USER).one()
    db_session.add(UserRole(user_id=user.id, role_id=registered_role.id))
    db_session.commit()

    perms = get_effective_permissions(db_session, user)
    assert "events.review" in perms  # from Editor
    assert "events.correct_location" in perms  # from Editor
    assert "users.view" not in perms  # Registered User adds no permissions
    assert "events.create" not in perms  # no longer a valid permission at all
    assert "events.archive" not in perms  # Editor doesn't get this (Administrator+ only)
    assert "roles.manage" not in perms  # neither role grants this


def test_effective_permissions_page_for_super_admin(
    client, make_super_admin, make_user, login, db_session
):
    make_super_admin(email="root2@example.com", password="root-pass-1234")
    target = make_user(
        email="target2@example.com", password="pw-target12345", role_name=REGISTERED_USER
    )
    login("root2@example.com", "root-pass-1234")

    resp = client.get(f"/admin/users/{target.id}/effective-permissions")
    assert resp.status_code == 200
    assert REGISTERED_USER in resp.text
    assert "users.view" not in resp.text
