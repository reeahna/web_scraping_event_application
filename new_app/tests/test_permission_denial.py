from app.core.permissions import REGISTERED_USER


def test_registered_user_denied_roles_management_page(client, make_user, login):
    make_user(
        email="registered@example.com",
        password="pw-registered12345",
        role_name=REGISTERED_USER,
    )
    login("registered@example.com", "pw-registered12345")

    resp = client.get("/admin/roles")
    assert resp.status_code == 403


def test_registered_user_denied_users_page(client, make_user, login):
    make_user(
        email="registered2@example.com",
        password="pw-registered12345",
        role_name=REGISTERED_USER,
    )
    login("registered2@example.com", "pw-registered12345")

    resp = client.get("/admin/users")
    assert resp.status_code == 403
