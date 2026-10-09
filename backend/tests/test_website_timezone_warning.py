"""The DST warning must not depend on having a working configuration.

The timezone override belongs to the website, but the warning used to render
inside a table gated on `website.configuration or website.approved_pattern`.
That hid it on unsupported and draft sources, which are exactly the ones an
administrator is in the middle of fixing.
"""

import pytest

from app.core.timezones import dst_warning


@pytest.fixture
def admin(make_super_admin, login):
    make_super_admin(email="tz-admin@example.com", password="root-pass-1234")
    login("tz-admin@example.com", "root-pass-1234")


def _detail(client, website):
    response = client.get(f"/admin/websites/{website.id}")
    assert response.status_code == 200
    return response.text


@pytest.mark.parametrize("status", ["draft", "unsupported", "needs_review"])
def test_warning_shows_on_a_site_with_no_configuration(
    client, admin, make_city, make_website, db_session, status
):
    website = make_website(make_city(), name=f"TZ {status}")
    website.onboarding_status = status
    website.timezone_override = "EST"
    website.configuration = None
    website.approved_pattern = None
    db_session.commit()

    body = _detail(client, website)
    assert "DST warning" in body
    assert "daylight saving" in body.lower()


def test_no_warning_for_a_regional_iana_zone(
    client, admin, make_city, make_website, db_session
):
    website = make_website(make_city(), name="TZ regional")
    website.onboarding_status = "unsupported"
    website.timezone_override = "America/New_York"
    db_session.commit()

    body = _detail(client, website)
    assert "DST warning" not in body
    assert "America/New_York" in body


def test_timezone_is_shown_even_with_no_override(
    client, admin, make_city, make_website, db_session
):
    website = make_website(make_city(), name="TZ inherited")
    website.onboarding_status = "draft"
    website.timezone_override = None
    db_session.commit()

    body = _detail(client, website)
    assert "(uses city timezone)" in body
    assert "DST warning" not in body


def test_the_helper_and_the_page_agree_on_which_zones_warn():
    """Guards against the page and the helper drifting apart."""
    assert dst_warning("EST")
    assert dst_warning("MST")
    assert dst_warning("America/New_York") is None
    assert dst_warning(None) is None
