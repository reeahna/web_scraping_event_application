import pytest

from app.core.permissions import ADMINISTRATOR, EDITOR, REGISTERED_USER, SUPER_ADMINISTRATOR


def test_account_browser_request_redirects_to_login(client):
    response = client.get("/account", headers={"accept": "text/html"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login?next=%2Faccount"


def test_account_api_request_gets_401(client):
    response = client.get("/account", headers={"accept": "application/json"})

    assert response.status_code == 401


@pytest.mark.parametrize("role_name", [REGISTERED_USER, ADMINISTRATOR])
def test_account_available_to_registered_users_and_administrators(
    client, make_user, login, role_name
):
    make_user(
        email=f"{role_name.lower().replace(' ', '-')}@example.com",
        password="account-pass-123",
        role_name=role_name,
    )
    email = f"{role_name.lower().replace(' ', '-')}@example.com"
    login(email, "account-pass-123")

    response = client.get("/account")
    assert response.status_code == 200
    assert email in response.text
    assert role_name in response.text
    for placeholder in ("Saved Events", "Followed Cities", "Alerts"):
        assert placeholder in response.text
    assert "not yet available" in response.text


def test_registered_user_account_has_no_admin_controls(client, make_user, login):
    make_user(
        email="plain-account@example.com",
        password="account-pass-123",
        role_name=REGISTERED_USER,
    )
    login("plain-account@example.com", "account-pass-123")

    response = client.get("/account")
    assert response.status_code == 200
    assert 'href="/admin"' not in response.text
    assert "role_id" not in response.text
    assert "permission" not in response.text.lower()


@pytest.mark.parametrize(
    ("role_name", "expected"),
    [
        (REGISTERED_USER, "/account"),
        (EDITOR, "/admin"),
        (ADMINISTRATOR, "/admin"),
        (SUPER_ADMINISTRATOR, "/admin"),
    ],
)
def test_normal_login_redirect_uses_effective_admin_access(make_user, login, role_name, expected):
    email = f"redirect-{role_name.lower().replace(' ', '-')}@example.com"
    make_user(email=email, password="redirect-pass-123", role_name=role_name)

    response = login(email, "redirect-pass-123")
    assert response.status_code == 303
    assert response.headers["location"] == expected


@pytest.mark.parametrize("next_path", ["/account", "/admin/users", "/account?tab=alerts"])
def test_safe_next_path_takes_priority(client, make_super_admin, next_path):
    make_super_admin(email="safe-next@example.com", password="redirect-pass-123")
    client.get("/auth/login")

    response = client.post(
        "/auth/login",
        data={
            "email": "safe-next@example.com",
            "password": "redirect-pass-123",
            "csrf_token": client.cookies.get("csrf_token"),
            "next": next_path,
        },
        follow_redirects=False,
    )
    assert response.headers["location"] == next_path


@pytest.mark.parametrize(
    "unsafe_next",
    [
        "https://evil.example/phish",
        "//evil.example/phish",
        "/\\evil.example/phish",
        "/%2f%2fevil.example/phish",
        "/admin%ZZ",
        "/admin#fragment",
        "admin/users",
    ],
)
def test_unsafe_or_malformed_next_falls_back_by_permissions(client, make_user, unsafe_next):
    make_user(
        email="unsafe-next@example.com",
        password="redirect-pass-123",
        role_name=REGISTERED_USER,
    )
    client.get("/auth/login")

    response = client.post(
        "/auth/login",
        data={
            "email": "unsafe-next@example.com",
            "password": "redirect-pass-123",
            "csrf_token": client.cookies.get("csrf_token"),
            "next": unsafe_next,
        },
        follow_redirects=False,
    )
    assert response.headers["location"] == "/account"
