"""review_status means "something flagged this", not "nobody has looked yet".

Every event used to be created as "needs_review", so the flag never separated
anything and the admin filter offered "all events" or "none". The only
automated writer is the geographic check, which is now the thing that puts an
event into the queue.
"""

from app.models.event import Event
from app.repositories.event import create_event
from app.schemas.event import EventCreate
from app.services.geographic_filter import geo_needs_review
from tests.test_8g_pipeline import _candidate


def test_a_newly_created_event_is_not_in_the_review_queue(db_session, make_city):
    city = make_city(name="Review City", slug="review-city")
    event = create_event(
        db_session,
        EventCreate(
            title="Trusted Event",
            canonical_url="https://example.com/trusted",
            source="Test Source",
            city_id=city.id,
        ),
    )
    assert event.review_status == "reviewed"


def test_the_model_default_is_reviewed(db_session, make_city):
    """Constructed without an explicit status — the column default applies."""
    city = make_city(name="Default City", slug="default-city")
    event = Event(
        title="Defaulted",
        canonical_url="https://example.com/defaulted",
        source="Test Source",
        city_id=city.id,
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)
    assert event.review_status == "reviewed"


def test_geo_check_is_still_what_puts_an_event_into_the_queue():
    """The flag's one automated writer must keep working — it is the whole
    reason the field is worth having now."""
    from app.services.geographic_filter import (
        GeographicFilterConfig,
        annotate_candidate_geography,
    )

    config = GeographicFilterConfig(
        localities=["Springfield"], missing_geography_action="needs_review"
    )
    located = annotate_candidate_geography(_candidate(address="Springfield, IL"), config)
    missing = annotate_candidate_geography(_candidate(address=None), config)

    assert not geo_needs_review(located)
    assert geo_needs_review(missing)


def test_review_status_still_does_not_hide_events_from_the_public_site(
    db_session, make_city, make_website, make_event
):
    """Documents the boundary: review is an internal triage flag, not a
    visibility gate. Only is_active/archived/duplicate control publication."""
    from datetime import timedelta

    from app.repositories.public_events import current_public_date, list_public_events

    city = make_city(name="Visible City", slug="visible-city")
    website = make_website(city, name="Visible Site")
    # Publication also requires an approved configuration and an active site;
    # this test is about review_status, so satisfy the rest explicitly.
    website.active_configuration_version = 1
    website.is_active = True
    db_session.commit()
    today = current_public_date()
    event = make_event(
        city,
        website=website,
        title="Flagged But Public",
        start_date=today + timedelta(days=3),
    )
    event.review_status = "needs_review"
    db_session.commit()

    events, _, _ = list_public_events(db_session, today=today)
    assert any(e.id == event.id for e in events)
