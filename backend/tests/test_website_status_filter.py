"""The websites status filter: grouped, labelled, counted, and multi-select.

The dropdown used to render the raw ONBOARDING_STATES tuple, so operators read
storage identifiers ("needs_review") in a flat list of ten near-identical words.
It is now a set of checkboxes, so several statuses can be shown, or hidden, at
once, while single-status links from before keep working.
"""

import re

from app.core.onboarding import ONBOARDING_STATES
from app.core.permissions import ADMINISTRATOR


def _admin(make_user, login):
    make_user(email="sites@example.com", password="sites-pass-123", role_name=ADMINISTRATOR)
    login("sites@example.com", "sites-pass-123")


def _status_select(html: str) -> str:
    return re.search(r'<details class="status-filter">.*?</details>', html, re.S).group(0)


def test_every_state_stays_selectable(client, make_user, login):
    """Grouping must not quietly drop a state — that would make those sites
    unreachable through the filter."""
    _admin(make_user, login)
    select = _status_select(client.get("/admin/websites").text)
    for state in ONBOARDING_STATES:
        assert f'value="{state}"' in select


def test_options_are_grouped_and_labelled_in_english(client, make_user, login):
    _admin(make_user, login)
    select = _status_select(client.get("/admin/websites").text)

    for group in ("Live", "Waiting on you", "Problem", "Retired"):
        assert f"<legend>{group}</legend>" in select

    # The identifier stays in the value; only the visible text is humanised.
    assert "Needs review (" in select
    assert ">needs_review<" not in select


def test_counts_include_zero_states(client, make_user, login, make_city, make_website, db_session):
    """A zero is worth showing: it answers "is anything failing?" without a
    second search, and keeps the option list stable between visits."""
    _admin(make_user, login)
    city = make_city(name="Count City", slug="count-city")
    for index in range(3):
        site = make_website(city, name=f"Counted {index}")
        site.onboarding_status = "active"
    db_session.commit()

    select = _status_select(client.get("/admin/websites").text)
    assert "Active (3)" in select
    assert "Failing (0)" in select


def test_status_badges_in_the_table_are_humanised(
    client, make_user, login, make_city, make_website, db_session
):
    _admin(make_user, login)
    city = make_city(name="Badge City", slug="badge-city")
    site = make_website(city, name="Badged Site")
    site.onboarding_status = "needs_review"
    db_session.commit()

    body = client.get("/admin/websites").text
    assert ">Needs review</span>" in body


def test_filtering_by_a_grouped_status_still_works(
    client, make_user, login, make_city, make_website, db_session
):
    _admin(make_user, login)
    city = make_city(name="Filter City", slug="filter-city")
    kept = make_website(city, name="Kept Site")
    kept.onboarding_status = "failing"
    other = make_website(city, name="Other Site")
    other.onboarding_status = "active"
    db_session.commit()

    body = client.get("/admin/websites?onboarding_status=failing").text
    assert "Kept Site" in body
    assert "Other Site" not in body


def _three_sites(make_city, make_website, db_session):
    city = make_city(name="Multi City", slug="multi-city")
    sites = (("Live Site", "active"), ("Broken Site", "failing"), ("Walled Site", "blocked"))
    for name, status in sites:
        site = make_website(city, name=name)
        site.onboarding_status = status
    db_session.commit()


def test_several_statuses_can_be_shown_at_once(
    client, make_user, login, make_city, make_website, db_session
):
    _admin(make_user, login)
    _three_sites(make_city, make_website, db_session)

    body = client.get("/admin/websites?onboarding_status=failing&onboarding_status=blocked").text
    assert "Broken Site" in body
    assert "Walled Site" in body
    assert "Live Site" not in body

    select = _status_select(body)
    assert 'value="failing" checked' in select
    assert 'value="blocked" checked' in select
    assert 'value="active" checked' not in select
    assert "Status: Failing, Blocked" in select


def test_checked_statuses_can_be_hidden_instead(
    client, make_user, login, make_city, make_website, db_session
):
    _admin(make_user, login)
    _three_sites(make_city, make_website, db_session)

    body = client.get(
        "/admin/websites?onboarding_status=active&onboarding_status=blocked&status_mode=hide"
    ).text
    assert "Broken Site" in body
    assert "Live Site" not in body
    assert "Walled Site" not in body

    select = _status_select(body)
    assert 'name="status_mode" value="hide" checked' in select
    assert "Hiding: Active, Blocked" in select


def test_hide_with_nothing_checked_is_no_filter(
    client, make_user, login, make_city, make_website, db_session
):
    _admin(make_user, login)
    _three_sites(make_city, make_website, db_session)

    body = client.get("/admin/websites?status_mode=hide&onboarding_status=").text
    for name in ("Live Site", "Broken Site", "Walled Site"):
        assert name in body
    assert "All statuses" in _status_select(body)


def test_pagination_links_keep_every_status(
    client, make_user, login, make_city, make_website, db_session
):
    """Page 2 must keep the same filter, so each status repeats in the link."""
    _admin(make_user, login)
    city = make_city(name="Paged City", slug="paged-city")
    for index in range(30):
        site = make_website(city, name=f"Paged {index:02d}")
        site.onboarding_status = "failing" if index % 2 else "blocked"
    db_session.commit()

    body = client.get(
        "/admin/websites?onboarding_status=failing&onboarding_status=blocked&status_mode=hide"
    ).text
    assert "No websites found." in body

    body = client.get("/admin/websites?onboarding_status=failing&onboarding_status=blocked").text
    assert "onboarding_status=failing&amp;onboarding_status=blocked&amp;page=2" in body
