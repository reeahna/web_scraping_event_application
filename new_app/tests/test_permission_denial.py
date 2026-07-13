from app.core.permissions import VIEWER


def test_viewer_denied_roles_management_page(client, make_user, login):
    make_user(email="viewer@example.com", password="pw-viewer12345", role_name=VIEWER)
    login("viewer@example.com", "pw-viewer12345")

    resp = client.get("/admin/roles")
    assert resp.status_code == 403


def test_viewer_can_view_users_page(client, make_user, login):
    make_user(email="viewer2@example.com", password="pw-viewer12345", role_name=VIEWER)
    login("viewer2@example.com", "pw-viewer12345")

    resp = client.get("/admin/users")
    assert resp.status_code == 200
