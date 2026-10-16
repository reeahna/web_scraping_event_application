"""Guards on role editing and on activating/deactivating privileged users."""

from app.core.permissions import (
    ADMINISTRATOR,
    EDITOR,
    PERMISSIONS,
    SUPER_ADMINISTRATOR,
)
from app.models.role import Role


def _csrf(client) -> str:
    return client.cookies.get("csrf_token")


def _role(db_session, name: str) -> Role:
    return db_session.query(Role).filter(Role.name == name).one()


def _codes(role: Role) -> set[str]:
    return {rp.permission.code for rp in role.role_permissions}


def _edit_role(client, role: Role, **overrides):
    data = {
        "name": role.name,
        "description": role.description or "",
        "is_active": "on",
        "permission_codes": sorted(_codes(role)),
        "csrf_token": _csrf(client),
    }
    data.update(overrides)
    return client.post(f"/admin/roles/{role.id}", data=data, follow_redirects=False)


def test_super_admin_role_permissions_cannot_be_stripped(
    client, make_super_admin, login, db_session
):
    make_super_admin(email="sa@example.com")
    login("sa@example.com")
    role = _role(db_session, SUPER_ADMINISTRATOR)

    resp = _edit_role(client, role, permission_codes=[])
    assert resp.status_code == 403

    db_session.expire_all()
    assert _codes(_role(db_session, SUPER_ADMINISTRATOR)) == set(PERMISSIONS)


def test_super_admin_role_description_can_still_be_edited(
    client, make_super_admin, login, db_session
):
    make_super_admin(email="sa@example.com")
    login("sa@example.com")
    role = _role(db_session, SUPER_ADMINISTRATOR)

    resp = _edit_role(client, role, description="Everything")
    assert resp.status_code == 303

    db_session.expire_all()
    assert _role(db_session, SUPER_ADMINISTRATOR).description == "Everything"


def test_built_in_roles_cannot_be_renamed(client, make_super_admin, login, db_session):
    make_super_admin(email="sa@example.com")
    login("sa@example.com")

    for name in (SUPER_ADMINISTRATOR, ADMINISTRATOR, EDITOR):
        role = _role(db_session, name)
        resp = _edit_role(client, role, name=f"{name} renamed")
        assert resp.status_code == 403

    db_session.expire_all()
    assert db_session.query(Role).filter(Role.name == SUPER_ADMINISTRATOR).count() == 1
    assert db_session.query(Role).filter(Role.name.like("% renamed")).count() == 0


def test_rename_to_existing_role_name_is_rejected(client, make_super_admin, login, db_session):
    make_super_admin(email="sa@example.com")
    login("sa@example.com")
    custom = Role(name="Custom")
    db_session.add(custom)
    db_session.commit()

    resp = _edit_role(client, custom, name=EDITOR)
    assert resp.status_code == 409

    db_session.expire_all()
    assert db_session.get(Role, custom.id).name == "Custom"


def test_unknown_permission_code_is_rejected(client, make_super_admin, login, db_session):
    make_super_admin(email="sa@example.com")
    login("sa@example.com")
    custom = Role(name="Custom")
    db_session.add(custom)
    db_session.commit()

    resp = _edit_role(client, custom, permission_codes=["events.view", "bogus.perm"])
    assert resp.status_code == 422

    db_session.expire_all()
    assert _codes(db_session.get(Role, custom.id)) == set()


def test_role_names_are_stripped_and_blank_rejected(client, make_super_admin, login, db_session):
    make_super_admin(email="sa@example.com")
    login("sa@example.com")

    resp = client.post(
        "/admin/roles",
        data={"name": "   ", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 422

    resp = client.post(
        "/admin/roles",
        data={"name": "  Curator  ", "csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    curator = _role(db_session, "Curator")

    resp = _edit_role(client, curator, name="  ")
    assert resp.status_code == 422


def test_administrator_cannot_deactivate_or_activate_super_admin(
    client, make_super_admin, make_user, login, db_session
):
    make_super_admin(email="sa1@example.com")
    sa2 = make_super_admin(email="sa2@example.com")
    make_user(email="admin@example.com", role_name=ADMINISTRATOR)
    login("admin@example.com")

    resp = client.post(
        f"/admin/users/{sa2.id}/deactivate",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    db_session.refresh(sa2)
    assert sa2.is_active is True

    sa2.is_active = False
    db_session.commit()
    resp = client.post(
        f"/admin/users/{sa2.id}/activate",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    db_session.refresh(sa2)
    assert sa2.is_active is False


def test_administrator_can_still_deactivate_ordinary_user(
    client, make_user, login, db_session
):
    make_user(email="admin@example.com", role_name=ADMINISTRATOR)
    editor = make_user(email="editor@example.com", role_name=EDITOR)
    login("admin@example.com")

    resp = client.post(
        f"/admin/users/{editor.id}/deactivate",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.refresh(editor)
    assert editor.is_active is False


def test_super_admin_can_deactivate_another_super_admin(
    client, make_super_admin, login, db_session
):
    make_super_admin(email="sa1@example.com")
    sa2 = make_super_admin(email="sa2@example.com")
    login("sa1@example.com")

    resp = client.post(
        f"/admin/users/{sa2.id}/deactivate",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.refresh(sa2)
    assert sa2.is_active is False
