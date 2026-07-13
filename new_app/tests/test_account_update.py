import json

import pytest

from app.core.permissions import ADMINISTRATOR, REGISTERED_USER
from app.models.audit_log import AuditLog
from app.models.permission import Permission
from app.models.role import Role
from app.models.role_permission import RolePermission
from app.models.user_role import UserRole


def _post_account(client, display_name: str, **extra_fields):
    data = {
        "display_name": display_name,
        "csrf_token": client.cookies.get("csrf_token"),
    }
    data.update(extra_fields)
    return client.post("/account", data=data, follow_redirects=False)


def test_display_name_is_read_only_by_default_with_accessible_pencil(
    client, make_user, login, db_session
):
    user = make_user(
        email="readonly-name@example.com",
        password="account-pass-123",
        role_name=REGISTERED_USER,
    )
    user.full_name = "Read Only Name"
    db_session.commit()
    login(user.email, "account-pass-123")

    response = client.get("/account")
    assert response.status_code == 200
    assert '<div id="display-name-readonly" class="display-name-readonly">' in response.text
    assert '<span id="display-name-text">Read Only Name</span>' in response.text
    assert 'id="display-name-edit-form" hidden' in response.text
    assert 'type="submit" name="edit" value="1"' in response.text
    assert 'aria-label="Edit display name"' in response.text
    assert '<span aria-hidden="true">✎</span>' in response.text


@pytest.mark.parametrize("role_name", [REGISTERED_USER, ADMINISTRATOR])
def test_edit_mode_shows_prefilled_form_in_registered_and_admin_layouts(
    client, make_user, login, db_session, role_name
):
    email = f"edit-mode-{role_name.lower().replace(' ', '-')}@example.com"
    user = make_user(
        email=email,
        password="account-pass-123",
        role_name=role_name,
    )
    user.full_name = "Existing Display Name"
    db_session.commit()
    login(user.email, "account-pass-123")

    response = client.get("/account?edit=1")
    assert response.status_code == 200
    assert 'id="display-name-readonly" class="display-name-readonly" hidden' in response.text
    assert 'id="display-name-edit-form">' in response.text
    assert 'id="display-name-edit-form" hidden' not in response.text
    assert 'value="Existing Display Name"' in response.text
    assert ">Save</button>" in response.text
    assert 'id="cancel-display-name-edit">Cancel</a>' in response.text


def test_cancel_link_returns_to_read_only_mode(client, make_user, login):
    user = make_user(
        email="cancel-edit@example.com",
        password="account-pass-123",
        role_name=REGISTERED_USER,
    )
    login(user.email, "account-pass-123")

    edit_page = client.get("/account?edit=1")
    assert 'href="/account" class="button-link btn-secondary"' in edit_page.text

    read_only_page = client.get("/account")
    assert 'id="display-name-edit-form" hidden' in read_only_page.text
    assert '<div id="display-name-readonly" class="display-name-readonly">' in read_only_page.text


def test_registered_user_can_edit_own_display_name(client, make_user, login, db_session):
    user = make_user(
        email="registered-name@example.com",
        password="account-pass-123",
        role_name=REGISTERED_USER,
    )
    login(user.email, "account-pass-123")

    response = _post_account(client, "  Updated Registered Name  ")
    assert response.status_code == 303
    assert response.headers["location"] == "/account"
    db_session.refresh(user)
    assert user.full_name == "Updated Registered Name"

    account = client.get("/account")
    assert "Display name updated successfully." in account.text
    assert '<span id="display-name-text">Updated Registered Name</span>' in account.text
    assert 'id="display-name-edit-form" hidden' in account.text
    assert 'value="Updated Registered Name"' in account.text


def test_admin_user_can_edit_own_display_name(client, make_super_admin, login, db_session):
    user = make_super_admin(email="admin-name@example.com", password="account-pass-123")
    login(user.email, "account-pass-123")

    response = _post_account(client, "Updated Administrator")
    assert response.status_code == 303
    db_session.refresh(user)
    assert user.full_name == "Updated Administrator"


@pytest.mark.parametrize("display_name", ["", "   \t  "])
def test_blank_display_name_is_rejected(client, make_user, login, db_session, display_name):
    user = make_user(
        email="blank-name@example.com",
        password="account-pass-123",
        role_name=REGISTERED_USER,
    )
    user.full_name = "Original Name"
    db_session.commit()
    login(user.email, "account-pass-123")

    response = _post_account(client, display_name)
    assert response.status_code == 422
    assert "Display name is required." in response.text
    assert 'id="display-name-edit-form" hidden' not in response.text
    assert 'aria-invalid="true"' in response.text
    assert 'aria-describedby="display_name_error"' in response.text
    assert 'id="display_name_error"' in response.text
    assert f'value="{display_name}"' in response.text
    db_session.refresh(user)
    assert user.full_name == "Original Name"


def test_overlong_display_name_is_rejected(client, make_user, login, db_session):
    user = make_user(
        email="long-name@example.com",
        password="account-pass-123",
        role_name=REGISTERED_USER,
    )
    login(user.email, "account-pass-123")

    response = _post_account(client, "x" * 256)
    assert response.status_code == 422
    assert "255 characters or fewer" in response.text
    assert 'id="display-name-edit-form" hidden' not in response.text
    assert f'value="{"x" * 256}"' in response.text
    db_session.refresh(user)
    assert user.full_name is None


def test_account_update_rejects_invalid_csrf(client, make_user, login, db_session):
    user = make_user(
        email="account-csrf@example.com",
        password="account-pass-123",
        role_name=REGISTERED_USER,
    )
    login(user.email, "account-pass-123")

    response = client.post(
        "/account",
        data={"display_name": "Injected Name", "csrf_token": "wrong-token"},
    )
    assert response.status_code == 403
    db_session.refresh(user)
    assert user.full_name is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("role_id", "1"),
        ("email", "attacker-controlled@example.com"),
        ("is_active", "false"),
    ],
)
def test_account_update_rejects_role_email_and_active_status_injection(
    client, make_user, login, db_session, field, value
):
    user = make_user(
        email="closed-account@example.com",
        password="account-pass-123",
        role_name=REGISTERED_USER,
    )
    original_role_ids = {assignment.role_id for assignment in user.user_roles}
    login(user.email, "account-pass-123")

    response = _post_account(client, "Injected Update", **{field: value})
    assert response.status_code == 422
    assert "Unexpected account fields" in response.text
    db_session.refresh(user)
    assert user.full_name is None
    assert user.email == "closed-account@example.com"
    assert user.is_active is True
    assert {assignment.role_id for assignment in user.user_roles} == original_role_ids


def test_display_name_change_creates_audit_record(client, make_user, login, db_session):
    user = make_user(
        email="account-audit@example.com",
        password="account-pass-123",
        role_name=REGISTERED_USER,
    )
    user.full_name = "Before Name"
    db_session.commit()
    login(user.email, "account-pass-123")

    response = _post_account(client, "After Name")
    assert response.status_code == 303

    entry = db_session.query(AuditLog).filter(AuditLog.action == "display_name_changed").one()
    assert entry.user_id == user.id
    assert entry.entity_id == user.id
    assert json.loads(entry.before_state) == {"display_name": "Before Name"}
    assert json.loads(entry.after_state) == {"display_name": "After Name"}


def test_registered_user_account_uses_public_navigation(client, make_user, login):
    user = make_user(
        email="public-account-nav@example.com",
        password="account-pass-123",
        role_name=REGISTERED_USER,
    )
    login(user.email, "account-pass-123")

    response = client.get("/account")
    assert response.status_code == 200
    assert '<a href="/" class="logo">New City Events</a>' in response.text
    assert '<a href="/account">My Account</a>' in response.text
    assert 'action="/auth/logout"' in response.text
    for admin_path in (
        "/admin",
        "/admin/cities",
        "/admin/websites",
        "/admin/users",
        "/admin/roles",
        "/admin/audit",
    ):
        assert f'href="{admin_path}"' not in response.text


def test_admin_account_uses_full_admin_navigation(client, make_super_admin, login):
    user = make_super_admin(email="full-admin-nav@example.com", password="account-pass-123")
    login(user.email, "account-pass-123")

    response = client.get("/account")
    assert response.status_code == 200
    expected_links = {
        "/admin": "Dashboard",
        "/admin/cities": "Cities",
        "/admin/websites": "Websites",
        "/admin/users": "Users",
        "/admin/roles": "Roles",
        "/admin/audit": "Audit Log",
        "/account": "My Account",
    }
    for path, label in expected_links.items():
        assert f'<a href="{path}">{label}</a>' in response.text
    assert user.email in response.text
    assert 'action="/auth/logout"' in response.text


def test_account_layout_uses_effective_permissions_not_known_role_name(
    client, make_user, login, db_session
):
    custom_role = Role(name="Custom City Reader", description="Custom permission role")
    db_session.add(custom_role)
    db_session.flush()
    permission = db_session.query(Permission).filter(Permission.code == "cities.view").one()
    db_session.add(RolePermission(role_id=custom_role.id, permission_id=permission.id))
    db_session.commit()
    user = make_user(email="custom-nav@example.com", password="account-pass-123")
    db_session.add(UserRole(user_id=user.id, role_id=custom_role.id))
    db_session.commit()
    login(user.email, "account-pass-123")

    response = client.get("/account")
    assert response.status_code == 200
    assert '<a href="/admin">Dashboard</a>' in response.text
    assert '<a href="/admin/cities">Cities</a>' in response.text


def test_account_and_public_navigation_have_no_separate_admin_dashboard_button(
    client, make_super_admin, login
):
    user = make_super_admin(email="no-button@example.com", password="account-pass-123")
    login(user.email, "account-pass-123")

    assert "Admin Dashboard" not in client.get("/account").text
    assert "Admin Dashboard" not in client.get("/").text


def test_email_is_read_only_on_account_form(client, make_user, login):
    user = make_user(
        email="readonly-email@example.com",
        password="account-pass-123",
        role_name=REGISTERED_USER,
    )
    login(user.email, "account-pass-123")

    response = client.get("/account")
    assert user.email in response.text
    assert 'name="email"' not in response.text
