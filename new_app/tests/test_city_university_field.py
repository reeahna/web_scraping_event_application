"""A college town's school must be settable through the admin.

The field was added to the model and the public chooser first; without the
admin form it would have been unsettable on a deployed site, so the chooser
would never show a school there.
"""

import pytest

from app.core.permissions import SUPER_ADMINISTRATOR
from app.models.city import City


@pytest.fixture
def admin(make_super_admin, login):
    make_super_admin(email="cities@example.com", password="cities-pass-123")
    login("cities@example.com", "cities-pass-123")


def _csrf(client):
    return client.cookies.get("csrf_token")


def test_the_create_form_offers_the_field(client, admin):
    html = client.get("/admin/cities/new").text
    assert 'name="university_name"' in html


def test_creating_a_town_stores_its_school(client, admin, db_session):
    response = client.post(
        "/admin/cities",
        data={
            "name": "Ann Arbor", "slug": "ann-arbor-mi",
            "state_or_region": "Michigan",
            "university_name": "University of Michigan",
            "country": "USA", "timezone": "America/Detroit",
            "default_latitude": "", "default_longitude": "", "boundary_config": "",
            "is_active": "on", "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:300]
    city = db_session.query(City).filter(City.slug == "ann-arbor-mi").one()
    assert city.university_name == "University of Michigan"


def test_editing_a_town_updates_its_school(client, admin, make_city, db_session):
    city = make_city(name="Bloomington", slug="bloomington-in", state_or_region="Indiana")
    db_session.commit()

    form = client.get(f"/admin/cities/{city.id}/edit").text
    assert 'name="university_name"' in form

    response = client.post(
        f"/admin/cities/{city.id}",
        data={
            "name": "Bloomington", "slug": "bloomington-in",
            "state_or_region": "Indiana",
            "university_name": "Indiana University",
            "country": "", "timezone": "UTC",
            "default_latitude": "", "default_longitude": "", "boundary_config": "",
            "is_active": "on", "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:300]
    db_session.refresh(city)
    assert city.university_name == "Indiana University"


def test_a_town_without_a_school_is_allowed(client, admin, db_session):
    """Not every town this covers has to have one."""
    response = client.post(
        "/admin/cities",
        data={
            "name": "Plainville", "slug": "plainville-xx",
            "state_or_region": "", "university_name": "",
            "country": "", "timezone": "UTC",
            "default_latitude": "", "default_longitude": "", "boundary_config": "",
            "is_active": "on", "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    city = db_session.query(City).filter(City.slug == "plainville-xx").one()
    assert city.university_name is None


def test_the_school_reaches_the_public_chooser(client, admin, make_city, db_session):
    """End to end: the point of the field."""
    make_city(
        name="Bloomington", slug="bloomington-in",
        university_name="Indiana University",
    )
    db_session.commit()
    assert "Indiana University" in client.get("/").text
