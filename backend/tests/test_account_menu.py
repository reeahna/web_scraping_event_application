"""The header's "My Account" dropdown, and the home page's nav.

Saved and Alerts moved from the top-level nav into a menu under My Account,
and the town chooser (the home page) no longer links to itself.
"""

import re

import pytest


@pytest.fixture
def signed_in(make_user, login, db_session):
    make_user(email="menu@example.com", password="menu-pass-1234")
    db_session.commit()
    login("menu@example.com", "menu-pass-1234")


def _header(html: str) -> str:
    return html.split("</header>", 1)[0]


def test_saved_and_alerts_sit_inside_the_account_menu(client, signed_in):
    header = _header(client.get("/").text)
    menu = re.search(r'<details class="account-menu">(.*?)</details>', header, re.S)
    assert menu, "the account dropdown is missing"
    inside = menu.group(1)
    assert "<summary" in inside and "My Account" in inside
    assert 'href="/account/saved"' in inside
    assert 'href="/account/inbox"' in inside
    assert 'href="/account"' in inside
    # Not repeated outside the menu.
    outside = header.replace(menu.group(0), "")
    assert 'href="/account/saved"' not in outside
    assert 'href="/account/inbox"' not in outside


def test_the_menu_script_is_external(client, signed_in):
    assert '<script src="/static/js/account-menu.js" defer></script>' in client.get("/").text


def test_signed_out_visitors_get_no_menu(client):
    html = client.get("/").text
    assert "account-menu" not in html


def test_home_page_does_not_link_to_itself(client):
    assert ">Find a town</a>" not in _header(client.get("/").text)


def test_other_pages_keep_the_find_a_town_link(client):
    assert '<a href="/">Find a town</a>' in _header(client.get("/privacy").text)
