"""Every admin filter bar offers "All …" options with an empty value.

Submitting the form with those selected sends `city_id=` (etc.), which
`int | None` cannot parse — FastAPI rejected the whole request with a 422
before the handler ran, so the Filter button appeared broken. app.core.forms
maps blank to None; these tests pin that down for each filter page.
"""

import pytest

from app.core.permissions import ADMINISTRATOR

# The exact query string each page's form submits with nothing narrowed down.
UNFILTERED_SUBMISSIONS = [
    (
        # `archived=no` because the events page defaults to hiding archived
        # events and the form renders that option selected on first load.
        "/admin/events",
        "q=&city_id=&website_id=&category_id=&active=all"
        "&archived=no&review_status=all&duplicate_status=all",
    ),
    ("/admin/websites", "q=&city_id=&onboarding_status="),
    ("/admin/unsupported-reports", "status=&city_id=&browser_required="),
]


@pytest.fixture
def filter_admin(make_user, login):
    make_user(email="filters@example.com", password="filter-pass-123", role_name=ADMINISTRATOR)
    login("filters@example.com", "filter-pass-123")


@pytest.mark.parametrize("path,query", UNFILTERED_SUBMISSIONS)
def test_blank_filter_values_are_treated_as_no_filter(client, filter_admin, path, query):
    response = client.get(f"{path}?{query}")
    assert response.status_code == 200, response.text[:300]


@pytest.mark.parametrize("path,query", UNFILTERED_SUBMISSIONS)
def test_unfiltered_submission_matches_the_bare_page(client, filter_admin, path, query):
    """Blank means "no filter", not "filter on nothing" — the result set must
    be identical to loading the page with no query string at all."""
    assert client.get(f"{path}?{query}").text == client.get(path).text


def test_a_real_id_still_filters(client, filter_admin, make_city, db_session):
    """The blank-to-None coercion must not swallow genuine values."""
    city = make_city(name="Filter City", slug="filter-city")
    db_session.commit()

    response = client.get(f"/admin/websites?city_id={city.id}")
    assert response.status_code == 200

    rejected = client.get("/admin/websites?city_id=not-a-number")
    assert rejected.status_code == 422
