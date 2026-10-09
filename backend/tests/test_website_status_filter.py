"""The websites status filter: grouped, labelled, and counted.

The dropdown used to render the raw ONBOARDING_STATES tuple, so operators read
storage identifiers ("needs_review") in a flat list of ten near-identical words.
"""

import re

from app.core.onboarding import ONBOARDING_STATES
from app.core.permissions import ADMINISTRATOR


def _admin(make_user, login):
    make_user(email="sites@example.com", password="sites-pass-123", role_name=ADMINISTRATOR)
    login("sites@example.com", "sites-pass-123")


def _status_select(html: str) -> str:
    return re.search(r'<select name="onboarding_status".*?</select>', html, re.S).group(0)


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

    assert '<optgroup label="Live">' in select
    assert '<optgroup label="Waiting on you">' in select
    assert '<optgroup label="Problem">' in select
    assert '<optgroup label="Retired">' in select

    # The identifier stays in the value; only the visible text is humanised.
    assert ">Needs review (" in select
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
    assert ">Active (3)<" in select
    assert ">Failing (0)<" in select


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
