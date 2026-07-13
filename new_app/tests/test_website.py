from app.repositories.city import create_city
from app.repositories.website import create_website
from app.schemas.city import CityCreate
from app.schemas.website import WebsiteCreate


def test_create_website_linked_to_city(db_session):
    city = create_city(db_session, CityCreate(name="Bethlehem, PA", slug="bethlehem-pa"))
    website = create_website(
        db_session,
        WebsiteCreate(
            name="Wind Creek Event Center",
            base_url="https://www.windcreekeventcenter.com/events",
            city_id=city.id,
        ),
    )
    assert website.id is not None
    assert website.city_id == city.id
    assert website.is_active is True
    assert website.requires_js is False
