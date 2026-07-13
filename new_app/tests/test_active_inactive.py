from app.repositories.city import create_city, list_cities
from app.schemas.city import CityCreate


def test_inactive_city_excluded_from_active_only_listing(db_session):
    create_city(db_session, CityCreate(name="Active City", slug="active-city", is_active=True))
    create_city(db_session, CityCreate(name="Inactive City", slug="inactive-city", is_active=False))

    active = list_cities(db_session, active_only=True)
    everything = list_cities(db_session, active_only=False)

    assert {c.slug for c in active} == {"active-city"}
    assert {c.slug for c in everything} == {"active-city", "inactive-city"}
