"""Admin header layout: brand row, then admin nav row, then flash below nav."""

from __future__ import annotations

import json


def _login_admin(client, make_super_admin, login, email):
    make_super_admin(email=email, password="root-pass-1234")
    login(email, "root-pass-1234")


def test_admin_brand_before_nav_before_content(client, make_super_admin, login):
    _login_admin(client, make_super_admin, login, "hdr1@example.com")
    body = client.get("/admin").text
    brand = body.index('class="logo">New City Events')
    nav = body.index('<nav class="admin-nav"')
    main = body.index('id="main-content"')
    # Brand precedes the admin navigation, which precedes the page content.
    assert brand < nav < main


def test_admin_flash_renders_below_the_navigation(client, make_super_admin, login):
    _login_admin(client, make_super_admin, login, "hdr2@example.com")
    client.cookies.set("flash", json.dumps({"category": "success", "message": "Saved."}))
    body = client.get("/admin").text

    header_close = body.index("</header>")
    nav = body.index('<nav class="admin-nav"')
    flash = body.index('class="flash-region"')
    # The admin nav is inside the header; the flash region is after the header,
    # so flash is below the complete navigation and outside the <nav> element.
    assert nav < header_close < flash
    # Flash message + styling preserved.
    assert 'flash flash-success' in body
    assert "Saved." in body


def test_admin_header_uses_left_aligned_admin_header_class(client, make_super_admin, login):
    _login_admin(client, make_super_admin, login, "hdr3@example.com")
    body = client.get("/admin").text
    assert '<header class="site-header admin-header">' in body


def test_admin_nav_preserves_account_and_notification_count(client, make_super_admin, login):
    _login_admin(client, make_super_admin, login, "hdr4@example.com")
    body = client.get("/admin").text
    assert 'action="/auth/logout"' in body
    assert 'href="/account"' in body
    # Unread notification count is still shown in the Notifications link.
    assert "Notifications (" in body


def test_public_header_is_unaffected(client, make_user, login):
    # Public pages keep the plain site-header with no admin nav row.
    body = client.get("/").text
    assert '<header class="site-header">' in body
    assert '<a href="/" class="logo">New City Events</a>' in body
    assert '<nav class="admin-nav"' not in body


def test_public_flash_still_renders_after_header(client):
    client.cookies.set("flash", json.dumps({"category": "error", "message": "Nope."}))
    body = client.get("/").text
    header_close = body.index("</header>")
    flash = body.index('class="flash-region"')
    assert header_close < flash
    assert "flash flash-error" in body
