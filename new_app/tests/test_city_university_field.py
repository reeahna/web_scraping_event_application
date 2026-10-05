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
    assert 'name="universities"' in html


def test_creating_a_town_stores_its_school(client, admin, db_session):
    response = client.post(
        "/admin/cities",
        data={
            "name": "Ann Arbor", "slug": "ann-arbor-mi",
            "state_or_region": "Michigan",
            "universities": "University of Michigan",
            "country": "USA", "timezone": "America/Detroit",
            "default_latitude": "", "default_longitude": "", "boundary_config": "",
            "is_active": "on", "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:300]
    city = db_session.query(City).filter(City.slug == "ann-arbor-mi").one()
    assert [u.name for u in city.universities] == ["University of Michigan"]


def test_editing_a_town_updates_its_school(client, admin, make_city, db_session):
    city = make_city(name="Bloomington", slug="bloomington-in", state_or_region="Indiana")
    db_session.commit()

    form = client.get(f"/admin/cities/{city.id}/edit").text
    assert 'name="universities"' in form

    response = client.post(
        f"/admin/cities/{city.id}",
        data={
            "name": "Bloomington", "slug": "bloomington-in",
            "state_or_region": "Indiana",
            "universities": "Indiana University",
            "country": "", "timezone": "UTC",
            "default_latitude": "", "default_longitude": "", "boundary_config": "",
            "is_active": "on", "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:300]
    db_session.refresh(city)
    assert [u.name for u in city.universities] == ["Indiana University"]


def test_a_town_without_a_school_is_allowed(client, admin, db_session):
    """Not every town this covers has to have one."""
    response = client.post(
        "/admin/cities",
        data={
            "name": "Plainville", "slug": "plainville-xx",
            "state_or_region": "", "universities": "",
            "country": "", "timezone": "UTC",
            "default_latitude": "", "default_longitude": "", "boundary_config": "",
            "is_active": "on", "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    city = db_session.query(City).filter(City.slug == "plainville-xx").one()
    assert city.universities == []


def test_the_school_reaches_the_public_chooser(client, admin, make_city, db_session):
    """End to end: the point of the field."""
    make_city(
        name="Bloomington", slug="bloomington-in",
        universities=["Indiana University"],
    )
    db_session.commit()
    assert "Indiana University" in client.get("/").text


# --- more than one school per town -------------------------------------------


def test_a_town_can_have_several_universities(client, admin, make_city, db_session):
    """State College has one; plenty of towns have more."""
    city = make_city(name="Claremont", slug="claremont-ca")
    db_session.commit()

    response = client.post(
        f"/admin/cities/{city.id}",
        data={
            "name": "Claremont", "slug": "claremont-ca",
            "state_or_region": "California",
            "universities": "Pomona College\nClaremont McKenna College\nHarvey Mudd College",
            "country": "", "timezone": "UTC",
            "default_latitude": "", "default_longitude": "", "boundary_config": "",
            "is_active": "on", "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:300]
    db_session.refresh(city)
    assert [u.name for u in city.universities] == [
        "Claremont McKenna College", "Harvey Mudd College", "Pomona College",
    ]  # ordered by name


def test_blank_lines_and_duplicates_are_dropped(client, admin, make_city, db_session):
    city = make_city(name="Dupe Town", slug="dupe-town")
    db_session.commit()

    client.post(
        f"/admin/cities/{city.id}",
        data={
            "name": "Dupe Town", "slug": "dupe-town", "state_or_region": "",
            "universities": "  A College  \n\n\nA College\nB College\n   \n",
            "country": "", "timezone": "UTC",
            "default_latitude": "", "default_longitude": "", "boundary_config": "",
            "is_active": "on", "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    db_session.refresh(city)
    assert [u.name for u in city.universities] == ["A College", "B College"]


def test_removing_a_university_removes_the_row(client, admin, make_city, db_session):
    from app.models.city_university import CityUniversity

    city = make_city(
        name="Shrink Town", slug="shrink-town",
        universities=["Keep College", "Drop College"],
    )
    db_session.commit()

    client.post(
        f"/admin/cities/{city.id}",
        data={
            "name": "Shrink Town", "slug": "shrink-town", "state_or_region": "",
            "universities": "Keep College",
            "country": "", "timezone": "UTC",
            "default_latitude": "", "default_longitude": "", "boundary_config": "",
            "is_active": "on", "csrf_token": _csrf(client),
        },
        follow_redirects=False,
    )
    db_session.refresh(city)
    assert [u.name for u in city.universities] == ["Keep College"]
    # Orphaned rather than left behind, so a dropped school cannot still match
    # a search.
    assert db_session.query(CityUniversity).filter(
        CityUniversity.name == "Drop College"
    ).count() == 0


def test_the_view_page_shows_the_universities(client, admin, make_city, db_session):
    """They were only ever visible on the edit form."""
    city = make_city(
        name="Viewable", slug="viewable-town",
        universities=["First College", "Second College"],
    )
    db_session.commit()

    html = client.get(f"/admin/cities/{city.id}").text
    assert "First College" in html
    assert "Second College" in html


def test_searching_finds_a_town_by_any_of_its_schools(client, make_city, db_session):
    make_city(
        name="Claremont", slug="claremont-ca",
        universities=["Pomona College", "Harvey Mudd College"],
    )
    db_session.commit()

    for term in ("pomona", "harvey", "claremont"):
        assert "Claremont" in client.get(f"/?q={term}").text, term


def test_a_town_matching_on_two_schools_appears_once(client, make_city, db_session):
    """A join would return the town twice; the query uses EXISTS."""
    import re

    make_city(
        name="Collegeville", slug="collegeville-pa",
        universities=["Ursinus College", "Collegeville College"],
    )
    db_session.commit()

    html = client.get("/?q=college").text
    grid = re.search(r'<ul class="city-grid">(.*?)</ul>', html, re.S).group(1)
    assert grid.count('class="city-card"') == 1
