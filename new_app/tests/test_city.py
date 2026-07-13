from app.repositories.city import create_city, get_city_by_slug
from app.schemas.city import CityCreate


def test_create_city(db_session):
    city = create_city(db_session, CityCreate(name="Bloomington Area, IN", slug="bloomington-in"))
    assert city.id is not None
    assert city.is_active is True
    assert city.created_at is not None

    fetched = get_city_by_slug(db_session, "bloomington-in")
    assert fetched is not None
    assert fetched.name == "Bloomington Area, IN"
