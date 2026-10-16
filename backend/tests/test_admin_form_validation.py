"""Admin city/website forms reject bad input as form errors, never as a 500."""

import pytest

from app.models.city import City
from app.models.website import Website


def _csrf(client) -> str:
    return client.cookies.get("csrf_token")


@pytest.fixture
def admin(client, make_super_admin, login):
    make_super_admin(email="root@example.com")
    login("root@example.com")
    client.get("/admin/cities/new")
    return client


def _city_form(client, **values) -> dict:
    data = {"name": "Ithaca", "slug": "ithaca-ny", "timezone": "UTC"}
    data.update(values)
    data["csrf_token"] = _csrf(client)
    return data


def _website_form(client, **values) -> dict:
    data = {"name": "Site", "base_url": "https://site-one.example.org"}
    data.update(values)
    data["csrf_token"] = _csrf(client)
    return data


# --- Cities ---------------------------------------------------------------------------


def test_duplicate_city_name_on_create_is_a_form_error(admin, make_city, db_session):
    make_city(name="Ithaca", slug="ithaca")
    resp = admin.post("/admin/cities", data=_city_form(admin, name="ITHACA"))
    assert resp.status_code == 409
    assert "A city with this name already exists." in resp.text
    assert db_session.query(City).filter(City.slug == "ithaca-ny").first() is None


def test_duplicate_city_name_on_update_is_a_form_error(admin, make_city):
    make_city(name="Ithaca", slug="ithaca")
    other = make_city(name="Elmira", slug="elmira")
    resp = admin.post(
        f"/admin/cities/{other.id}", data=_city_form(admin, name="ithaca", slug="elmira")
    )
    assert resp.status_code == 409
    assert "A city with this name already exists." in resp.text


def test_city_update_keeping_its_own_name_is_allowed(admin, make_city):
    city = make_city(name="Ithaca", slug="ithaca")
    resp = admin.post(
        f"/admin/cities/{city.id}",
        data=_city_form(admin, name="Ithaca", slug="ithaca"),
        follow_redirects=False,
    )
    assert resp.status_code == 303


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("default_latitude", "999"),
        ("default_latitude", "nan"),
        ("default_latitude", "-90.5"),
        ("default_longitude", "181"),
        ("default_longitude", "inf"),
    ],
)
def test_out_of_range_coordinates_rejected(admin, db_session, field, value):
    resp = admin.post("/admin/cities", data=_city_form(admin, **{field: value}))
    assert resp.status_code == 422
    assert "must be a number between" in resp.text
    assert db_session.query(City).filter(City.slug == "ithaca-ny").first() is None


def test_in_range_coordinates_accepted(admin, db_session):
    resp = admin.post(
        "/admin/cities",
        data=_city_form(admin, default_latitude="42.44", default_longitude="-76.5"),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    city = db_session.query(City).filter(City.slug == "ithaca-ny").one()
    assert city.default_latitude == pytest.approx(42.44)


def test_non_numeric_latitude_is_reported_on_latitude_not_boundary(admin):
    resp = admin.post("/admin/cities", data=_city_form(admin, default_latitude="north"))
    assert resp.status_code == 422
    assert "Must be a number, or left blank" in resp.text
    assert "Must be valid JSON" not in resp.text


def test_bad_boundary_json_still_reported_on_boundary(admin):
    resp = admin.post("/admin/cities", data=_city_form(admin, boundary_config="{nope"))
    assert resp.status_code == 422
    assert "Must be valid JSON, or left blank" in resp.text


def test_city_pagination_links_url_encode_query(admin, make_city):
    for i in range(25):
        make_city(name=f"R&B Town {i:02d}", slug=f"rb-town-{i:02d}")
    resp = admin.get("/admin/cities", params={"q": "R&B"})
    assert resp.status_code == 200
    assert "q=R%26B" in resp.text
    assert "q=R&amp;B&amp;" not in resp.text


# --- Websites -------------------------------------------------------------------------


def test_website_create_with_unknown_city_is_a_form_error(admin, db_session):
    resp = admin.post("/admin/websites", data=_website_form(admin, city_id="99999"))
    assert resp.status_code == 422
    assert "Choose a city from the list" in resp.text
    assert db_session.query(Website).count() == 0


def test_website_update_with_unknown_city_is_a_form_error(admin, make_city, make_website):
    site = make_website(make_city(), base_url="https://site-one.example.org")
    resp = admin.post(f"/admin/websites/{site.id}", data=_website_form(admin, city_id="99999"))
    assert resp.status_code == 422
    assert "Choose a city from the list" in resp.text


def test_non_numeric_city_id_is_reported_on_city_not_schedule(admin):
    resp = admin.post("/admin/websites", data=_website_form(admin, city_id="abc"))
    assert resp.status_code == 422
    assert "Choose a city from the list" in resp.text
    assert "Must be valid JSON" not in resp.text


def test_invalid_schedule_config_rejected_on_create(admin, db_session):
    resp = admin.post(
        "/admin/websites",
        data=_website_form(admin, schedule_config='{"interval_minutes": -5}'),
    )
    assert resp.status_code == 422
    assert "Invalid schedule" in resp.text
    assert "interval_minutes" in resp.text
    assert db_session.query(Website).count() == 0


def test_invalid_schedule_config_rejected_on_update(
    admin, make_city, make_website, db_session
):
    site = make_website(make_city(), base_url="https://site-one.example.org")
    resp = admin.post(
        f"/admin/websites/{site.id}",
        data=_website_form(admin, schedule_config='{"bogus": 1}'),
    )
    assert resp.status_code == 422
    assert "Invalid schedule" in resp.text
    db_session.refresh(site)
    assert site.schedule_config is None


def test_valid_schedule_config_saved(admin, db_session):
    resp = admin.post(
        "/admin/websites",
        data=_website_form(admin, schedule_config='{"interval_minutes": 60}'),
        follow_redirects=False,
    )
    assert resp.status_code == 303
    site = db_session.query(Website).one()
    assert site.schedule_config == {"interval_minutes": 60}


def test_website_update_cannot_take_another_websites_base_url(
    admin, make_city, make_website, db_session
):
    city = make_city()
    make_website(city, name="First", base_url="https://first.example.org")
    second = make_website(city, name="Second", base_url="https://second.example.org")
    resp = admin.post(
        f"/admin/websites/{second.id}",
        data=_website_form(admin, name="Second", base_url="https://first.example.org"),
    )
    assert resp.status_code == 409
    assert "already covers this URL" in resp.text
    db_session.refresh(second)
    assert second.base_url == "https://second.example.org"


def test_website_update_keeping_its_own_base_url_is_allowed(admin, make_city, make_website):
    site = make_website(make_city(), name="Only", base_url="https://only.example.org")
    resp = admin.post(
        f"/admin/websites/{site.id}",
        data=_website_form(admin, name="Renamed", base_url="https://only.example.org"),
        follow_redirects=False,
    )
    assert resp.status_code == 303


def test_website_pagination_links_url_encode_query(admin, make_city, make_website):
    city = make_city()
    for i in range(25):
        make_website(city, name=f"R&B Site {i:02d}", base_url=f"https://rb{i:02d}.example.org")
    resp = admin.get("/admin/websites", params={"q": "R&B"})
    assert resp.status_code == 200
    assert "q=R%26B" in resp.text
    assert "q=R&amp;B&amp;" not in resp.text
