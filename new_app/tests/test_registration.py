import json

import pytest
from pydantic import ValidationError

from app.config import get_settings
from app.core.permissions import ELEVATED_ROLES, REGISTERED_USER
from app.core.security import verify_password
from app.models.audit_log import AuditLog
from app.models.user import User
from app.models.user_role import UserRole
from app.schemas.registration import RegistrationCreate


def test_registration_page_loads_with_accessible_fields(client):
    response = client.get("/register")

    assert response.status_code == 200
    for field_id in ("display_name", "email", "password", "password_confirm"):
        assert f'for="{field_id}"' in response.text
        assert f'id="{field_id}"' in response.text


def test_successful_registration_creates_active_normalized_account_and_session(
    register, db_session
):
    response = register(display_name="  Casey Smith  ", email="  Casey@Example.COM ")

    assert response.status_code == 303
    assert response.headers["location"] == "/account"
    assert "session_token" in response.cookies

    user = db_session.query(User).filter(User.email == "casey@example.com").one()
    assert user.full_name == "Casey Smith"
    assert user.is_active is True
    assert user.last_login_at is not None
    assert user.hashed_password != "registration-pass-123"
    assert verify_password("registration-pass-123", user.hashed_password)

    role_names = {assignment.role.name for assignment in user.user_roles}
    assert role_names == {REGISTERED_USER}
    assert role_names.isdisjoint(ELEVATED_ROLES)


def test_immediate_registration_login_can_open_account(register, client):
    register(display_name="Jamie", email="jamie@example.com")

    response = client.get("/account")
    assert response.status_code == 200
    assert "jamie@example.com" in response.text
    assert REGISTERED_USER in response.text


@pytest.mark.parametrize("duplicate_email", ["dupe@example.com", "  DUPE@EXAMPLE.COM "])
def test_duplicate_email_is_rejected_after_normalization(register, db_session, duplicate_email):
    first = register(email="Dupe@Example.com")
    assert first.status_code == 303

    # Registration logged the first user in, but the endpoint remains public.
    second = register(email=duplicate_email)
    assert second.status_code == 409
    assert "already exists" in second.text
    assert db_session.query(User).filter(User.email == "dupe@example.com").count() == 1


def test_password_confirmation_must_match(register, db_session):
    response = register(password_confirm="different-password")

    assert response.status_code == 422
    assert "Passwords do not match" in response.text
    assert db_session.query(User).count() == 0


@pytest.mark.parametrize(
    ("password", "message"),
    [
        ("short", "at least"),
        ("        ", "required"),
        ("x" * 73, "at most 72 bytes"),
    ],
)
def test_password_policy_rejects_invalid_passwords(register, db_session, password, message):
    response = register(password=password, password_confirm=password)

    assert response.status_code == 422
    assert message in response.text
    assert db_session.query(User).count() == 0


def test_registration_rejects_mismatched_csrf(client, db_session):
    client.get("/register")
    response = client.post(
        "/register",
        data={
            "display_name": "CSRF Test",
            "email": "csrf@example.com",
            "password": "registration-pass-123",
            "password_confirm": "registration-pass-123",
            "csrf_token": "wrong-token",
        },
    )

    assert response.status_code == 403
    assert db_session.query(User).count() == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("role_id", "1"),
        ("role_name", "Super Administrator"),
        ("permission_ids", "1"),
        ("is_admin", "true"),
        ("is_active", "false"),
    ],
)
def test_registration_rejects_privilege_and_state_injection(register, db_session, field, value):
    response = register(**{field: value})

    assert response.status_code == 422
    assert "Unexpected registration fields" in response.text
    assert db_session.query(User).count() == 0
    assert db_session.query(UserRole).count() == 0


def test_registration_schema_forbids_arbitrary_model_fields():
    with pytest.raises(ValidationError):
        RegistrationCreate(
            display_name="Schema Test",
            email="schema@example.com",
            password="registration-pass-123",
            password_confirm="registration-pass-123",
            is_staff=True,
        )


def test_registration_disabled_hides_links_and_rejects_get_and_post(
    client, register, monkeypatch, db_session
):
    monkeypatch.setattr(get_settings(), "registration_enabled", False)

    assert 'href="/register"' not in client.get("/").text
    assert 'href="/register"' not in client.get("/auth/login").text
    assert client.get("/register").status_code == 404
    assert register(email="disabled@example.com").status_code == 404
    assert db_session.query(User).count() == 0


def test_registration_audit_has_expected_events_and_no_secrets(register, db_session):
    password = "unique-secret-pass-987"
    register(email="audit-registration@example.com", password=password, password_confirm=password)

    entries = db_session.query(AuditLog).order_by(AuditLog.id).all()
    assert {entry.action for entry in entries} == {
        "user_registered",
        "default_registered_user_role_assigned",
        "login_after_registration",
    }
    serialized = json.dumps(
        [
            {
                "detail": entry.detail,
                "before": entry.before_state,
                "after": entry.after_state,
            }
            for entry in entries
        ]
    )
    assert password not in serialized
    for forbidden in ("password", "session_token", "csrf_token", "cookie"):
        assert forbidden not in serialized.lower()


def test_normalized_email_can_be_used_for_shared_login(register, client):
    register(email="MixedCase@Example.com")
    client.cookies.clear()
    client.get("/auth/login")
    csrf = client.cookies.get("csrf_token")

    response = client.post(
        "/auth/login",
        data={
            "email": "  MIXEDCASE@EXAMPLE.COM ",
            "password": "registration-pass-123",
            "csrf_token": csrf,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/account"
