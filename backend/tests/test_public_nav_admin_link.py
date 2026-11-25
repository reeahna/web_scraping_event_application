"""The public header shows the Admin link on every page for anyone who may
open the admin, and never for anyone else.

Some routes used to work out `can_access_admin` and others did not, so an admin
saw the link on the home page but not on, say, Saved or the feedback page.
`render()` now fills it in when a route leaves it out.
"""

import pytest

from app.core.permissions import ADMINISTRATOR, REGISTERED_USER

PAGES = [
    "/",
    "/feedback",
    "/suggest",
    # /account is left out: an admin sees it in the admin shell, not this header.
    "/account/saved",
    "/account/inbox",
    "/whats-new",
    "/privacy",
    "/no-such-page",  # the error page
]


def _nav(html: str) -> str:
    return html[html.index('aria-label="Primary"'):html.index("</nav>")]


@pytest.mark.parametrize("path", PAGES)
def test_admin_sees_the_admin_link_everywhere(client, make_user, login, path):
    make_user(email="nav-admin@example.com", role_name=ADMINISTRATOR)
    login("nav-admin@example.com")
    response = client.get(path, headers={"accept": "text/html"})
    assert response.status_code in (200, 404)
    nav = _nav(response.text)
    assert '<a href="/admin">Admin</a>' in nav
    # The error page knows who is signed in too.
    assert "Log Out" in nav


@pytest.mark.parametrize("path", PAGES)
def test_registered_user_never_sees_it(client, make_user, login, path):
    make_user(email="nav-user@example.com", role_name=REGISTERED_USER)
    login("nav-user@example.com")
    response = client.get(path, headers={"accept": "text/html"})
    assert '<a href="/admin">Admin</a>' not in _nav(response.text)


def test_signed_out_visitor_never_sees_it(client):
    for path in ("/", "/feedback", "/privacy", "/no-such-page"):
        nav = _nav(client.get(path, headers={"accept": "text/html"}).text)
        assert '<a href="/admin">Admin</a>' not in nav
        assert "Sign in" in nav
