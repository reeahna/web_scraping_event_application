"""The grouped admin navigation.

Fifteen flat links became four hover/focus menus plus Dashboard. The menus open
from CSS, so the important guarantees here are structural: every destination is
still reachable, the triggers are buttons rather than links, the current page is
marked, and the behaviour script is external (an inline one would be blocked by
the app's Content-Security-Policy and silently never run).
"""

import re

import pytest

from app.core.permissions import ADMINISTRATOR

ADMIN_DESTINATIONS = [
    "/admin",
    "/admin/cities",
    "/admin/websites",
    "/admin/events",
    "/admin/event-categories",
    "/admin/categorization-rules",
    "/admin/onboarding/batches",
    "/admin/scheduler",
    "/admin/settings/onboarding-policies",
    "/admin/unsupported-reports",
    "/admin/users",
    "/admin/roles",
    "/admin/reports",
    "/admin/audit",
    "/admin/notifications",
    "/account",
]


@pytest.fixture
def nav_html(client, make_user, login):
    make_user(email="nav@example.com", password="nav-pass-1234", role_name=ADMINISTRATOR)
    login("nav@example.com", "nav-pass-1234")
    return client.get("/admin").text


@pytest.mark.parametrize("href", ADMIN_DESTINATIONS)
def test_every_destination_survived_the_regrouping(nav_html, href):
    """Grouping must not strand a page — nothing may become unreachable."""
    assert re.search(rf'<a href="{re.escape(href)}"[^>]*>', nav_html), href


def test_group_triggers_are_buttons_not_links(nav_html):
    """A group heading goes nowhere, so it must not look like a destination to
    assistive tech or open in a new tab on middle-click."""
    for heading in ("Sources", "Events", "Imports", "Administration"):
        assert f'<button type="button" class="admin-nav-trigger" aria-expanded="false">{heading}</button>' in nav_html
        assert f'>{heading}</a>' not in nav_html


def test_current_page_is_marked(client, make_user, login):
    make_user(email="nav-current@example.com", password="nav-pass-1234", role_name=ADMINISTRATOR)
    login("nav-current@example.com", "nav-pass-1234")

    html = client.get("/admin/websites").text
    nav = re.search(r'<nav class="admin-nav".*?</nav>', html, re.S).group(0)
    assert re.search(r'<a href="/admin/websites"[^>]*aria-current="page"', nav)
    # Exactly one nav item is marked. Scoped to the nav on purpose: the
    # pagination control legitimately marks its own current page too.
    assert nav.count('aria-current="page"') == 1


def test_behaviour_script_is_external_so_the_csp_cannot_block_it(nav_html):
    """`script-src 'self'` with no 'unsafe-inline' means an inline <script> or
    an onclick= attribute is refused by the browser and never runs."""
    assert '<script src="/static/js/admin-nav.js" defer></script>' in nav_html
    assert "<script>" not in nav_html


def test_no_inline_event_handlers_anywhere_in_the_admin_chrome(nav_html):
    assert not re.search(r'\son(click|submit|change|input)=', nav_html)
