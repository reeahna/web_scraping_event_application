"""Where an administrator lands after signing in: the Dashboard, unless they
were sent to sign in on the way to a particular admin page."""

import re

import pytest

from app.core.permissions import ADMINISTRATOR, REGISTERED_USER


@pytest.fixture
def admin(make_user):
    return make_user(email="landing-admin@example.com", role_name=ADMINISTRATOR)


def test_an_admin_with_nowhere_to_return_to_lands_on_the_dashboard(client, admin, login):
    resp = login("landing-admin@example.com")
    assert resp.headers["location"] == "/admin"

    page = client.get("/admin").text
    assert "<title>Admin Dashboard" in page
    assert re.search(r'href="/admin"\s+aria-current="page">Dashboard</a>', page)
    # Websites stays one click away in the admin menu, and is not the current page.
    assert re.search(r'href="/admin/websites"\s*>Websites</a>', page)


def test_an_admin_sent_to_sign_in_from_a_page_goes_back_to_it(client, admin, login):
    """Opening /admin/websites signed out asks for sign-in with next set."""
    first = client.get("/admin/websites", headers={"accept": "text/html"},
                       follow_redirects=False)
    assert first.headers["location"] == "/auth/login?next=%2Fadmin%2Fwebsites"

    resp = login("landing-admin@example.com", next="/admin/websites")
    assert resp.headers["location"] == "/admin/websites"


@pytest.mark.parametrize("unsafe", ["https://evil.example/admin", "//evil.example", "/\\evil"])
def test_an_off_site_next_falls_back_to_the_dashboard(admin, login, unsafe):
    assert login("landing-admin@example.com", next=unsafe).headers["location"] == "/admin"


def test_a_regular_user_still_lands_on_the_home_page(make_user, login):
    make_user(email="landing-member@example.com", role_name=REGISTERED_USER)
    assert login("landing-member@example.com").headers["location"] == "/"
