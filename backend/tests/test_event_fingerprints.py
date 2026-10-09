from app.models.event import Event
from app.services.fingerprints import (
    event_fingerprint,
    normalize_text,
    normalize_url,
    update_fingerprint_and_duplicates,
)


def test_fingerprint_stability_and_normalization(make_city):
    city = make_city()
    first = Event(
        title="  Summer   FESTIVAL ",
        canonical_url="",
        source="Source",
        city_id=city.id,
        venue=" Main   Hall ",
    )
    second = Event(
        title="summer festival",
        canonical_url="",
        source="Source",
        city_id=city.id,
        venue="main hall",
    )
    assert event_fingerprint(first) == event_fingerprint(second)
    assert normalize_text(first.title) == "summer festival"


def test_external_id_precedence(make_city, make_website):
    city = make_city()
    website = make_website(city)
    first = Event(
        title="First title",
        canonical_url="https://example.com/one",
        source="Source",
        city_id=city.id,
        website_id=website.id,
        external_source_id="stable-42",
    )
    second = Event(
        title="Changed title",
        canonical_url="https://example.com/two",
        source="Source",
        city_id=city.id,
        website_id=website.id,
        external_source_id="stable-42",
    )
    assert event_fingerprint(first) == event_fingerprint(second)


def test_canonical_url_precedence_and_normalization(make_city):
    city = make_city()
    first = Event(
        title="One",
        canonical_url="HTTPS://Example.COM/events/42/?b=2&a=1#fragment",
        source="Source",
        city_id=city.id,
    )
    second = Event(
        title="Different",
        canonical_url="https://example.com/events/42?a=1&b=2",
        source="Source",
        city_id=city.id,
    )
    assert normalize_url(first.canonical_url) == normalize_url(second.canonical_url)
    assert event_fingerprint(first) == event_fingerprint(second)


def test_composite_fallback_changes_with_occurrence_date(make_city):
    from datetime import date

    city = make_city()
    first = Event(
        title="Recurring Event",
        canonical_url="",
        source="Source",
        city_id=city.id,
        start_date=date(2026, 7, 1),
    )
    second = Event(
        title="Recurring Event",
        canonical_url="",
        source="Source",
        city_id=city.id,
        start_date=date(2026, 7, 2),
    )
    assert event_fingerprint(first) != event_fingerprint(second)


def test_likely_duplicates_are_flagged_but_not_merged(db_session, make_city):
    city = make_city()
    first = Event(
        title="Duplicate Event",
        canonical_url="https://example.com/same",
        source="Source",
        city_id=city.id,
    )
    second = Event(
        title="Duplicate Event Renamed",
        canonical_url="https://example.com/same",
        source="Source",
        city_id=city.id,
    )
    db_session.add_all([first, second])
    db_session.commit()
    update_fingerprint_and_duplicates(db_session, first)
    matches = update_fingerprint_and_duplicates(db_session, second)
    db_session.refresh(first)
    db_session.refresh(second)
    assert [event.id for event in matches] == [first.id]
    assert first.duplicate_status == "possible_duplicate"
    assert second.duplicate_status == "possible_duplicate"
    assert db_session.query(Event).count() == 2
