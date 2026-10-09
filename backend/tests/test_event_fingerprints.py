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


def _listing(db_session, city, title, url, **values):
    from datetime import date

    event = Event(
        title=title, canonical_url=url, source="Source", city_id=city.id,
        start_date=values.pop("start_date", date(2030, 6, 1)), **values,
    )
    db_session.add(event)
    db_session.commit()
    update_fingerprint_and_duplicates(db_session, event)
    return event


def test_same_event_from_another_source_is_flagged(db_session, make_city):
    city = make_city()
    first = _listing(db_session, city, "Heist", "https://theatre.example/heist")
    second = _listing(db_session, city, "HEIST!", "https://bureau.example/heist/58934")
    db_session.refresh(first)
    db_session.refresh(second)
    assert first.duplicate_status == "possible_duplicate"
    assert second.duplicate_status == "possible_duplicate"


def test_different_showtimes_or_days_are_not_flagged(db_session, make_city):
    from datetime import date, time

    city = make_city()
    events = [
        _listing(db_session, city, "Annie", "https://x/1", start_time=time(14)),
        _listing(db_session, city, "Annie", "https://x/2", start_time=time(19)),
        _listing(db_session, city, "Annie", "https://x/3", start_date=date(2030, 6, 2)),
    ]
    for event in events:
        db_session.refresh(event)
    assert {e.duplicate_status for e in events} == {"not_reviewed"}


def test_admin_resolution_survives_a_later_repeat_listing(db_session, make_city):
    city = make_city()
    first = _listing(db_session, city, "Heist", "https://theatre.example/heist")
    first.duplicate_status = "not_duplicate"
    db_session.commit()
    _listing(db_session, city, "Heist", "https://bureau.example/heist")
    db_session.refresh(first)
    assert first.duplicate_status == "not_duplicate"
