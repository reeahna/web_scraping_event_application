from datetime import date

from app.repositories.city import create_city
from app.repositories.event import create_event
from app.schemas.city import CityCreate
from app.schemas.event import EventCreate


def test_create_event_linked_to_city(db_session):
    city = create_city(db_session, CityCreate(name="Bloomington Area, IN", slug="bloomington-in"))
    event = create_event(
        db_session,
        EventCreate(
            title="Farmers Market",
            canonical_url="https://example.com/farmers-market",
            source="Test Source",
            start_date=date(2026, 8, 1),
            city_id=city.id,
        ),
    )
    assert event.id is not None
    assert event.city_id == city.id
    assert event.is_active is True
    assert event.created_at is not None
