"""The same event listed by several sources shows once on the public site."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from app.repositories.public_events import (
    get_public_event,
    list_public_events,
    list_public_sources,
    public_event_ids,
    upcoming_counts_by_city,
)
from app.services.fingerprints import title_key

TODAY = datetime.now(UTC).date()
TOMORROW = TODAY + timedelta(days=1)


def _site(make_website, city, name):
    return make_website(
        city, name=name, is_active=True, approved_pattern={"pattern_name": "static_html"},
        source_display_name=name,
    )


def _setup(make_city, make_website):
    city = make_city()
    return city, _site(make_website, city, "Theatre"), _site(make_website, city, "Visitors Bureau")


def _event(make_event, city, site, title, url, **values):
    return make_event(
        city, website=site, title=title, canonical_url=url,
        normalized_title=title_key(title), start_date=values.pop("start_date", TOMORROW),
        **values,
    )


def _listed(db_session, **filters):
    events, total, _ = list_public_events(db_session, today=TODAY, **filters)
    assert total == len(events)
    return [(e.title, e.website_id) for e in events]


def test_title_key_ignores_case_punctuation_and_entities():
    assert title_key("HEIST!") == title_key("Heist") == "heist"
    assert title_key("Rock &amp; Roll Night") == title_key("Rock and Roll Night")
    assert title_key("Don&#8217;t Drop the Ball") == title_key("Don’t Drop the Ball")
    assert title_key("Café Tacuba") == "cafe tacuba"


def test_same_event_from_two_sources_is_listed_once(
    db_session, make_city, make_website, make_event
):
    city, theatre, bureau = _setup(make_city, make_website)
    first = _event(make_event, city, theatre, "Heist", "https://theatre/heist")
    _event(make_event, city, bureau, "HEIST!", "https://bureau/heist",
           venue="Waldron Arts Center", start_time=time(0, 0))
    _event(make_event, city, bureau, "Other Show", "https://bureau/other")

    assert _listed(db_session) == [("Heist", theatre.id), ("Other Show", bureau.id)]
    assert upcoming_counts_by_city(db_session, today=TODAY) == {city.id: 2}
    assert first.id in public_event_ids(db_session, today=TODAY, limit=10)
    assert len(public_event_ids(db_session, today=TODAY, limit=10)) == 2


def test_the_hidden_copy_stays_reachable(db_session, make_city, make_website, make_event):
    city, theatre, bureau = _setup(make_city, make_website)
    _event(make_event, city, theatre, "Heist", "https://theatre/heist")
    copy = _event(make_event, city, bureau, "Heist", "https://bureau/heist")

    # At its own URL, and under its own source's filter.
    assert get_public_event(db_session, copy.id, today=TODAY) is not None
    assert _listed(db_session, source_id=bureau.id) == [("Heist", bureau.id)]
    assert {s.id for s in list_public_sources(db_session, today=TODAY)} == {
        theatre.id, bureau.id,
    }


def test_two_showings_at_different_times_both_stay(
    db_session, make_city, make_website, make_event
):
    city, theatre, _ = _setup(make_city, make_website)
    _event(make_event, city, theatre, "Annie", "https://theatre/annie-2pm", start_time=time(14))
    _event(make_event, city, theatre, "Annie", "https://theatre/annie-7pm", start_time=time(19))

    assert len(_listed(db_session)) == 2


def test_different_days_or_towns_are_not_repeats(
    db_session, make_city, make_website, make_event
):
    city, theatre, bureau = _setup(make_city, make_website)
    other_city = make_city(name="Other Town", slug="other-town")
    other_site = _site(make_website, other_city, "Other Theatre")
    _event(make_event, city, theatre, "Annie", "https://theatre/annie-1")
    _event(make_event, city, bureau, "Annie", "https://bureau/annie-2",
           start_date=TOMORROW + timedelta(days=1))
    _event(make_event, other_city, other_site, "Annie", "https://other/annie")

    assert len(_listed(db_session)) == 3


def test_a_hidden_earlier_copy_does_not_hide_the_visible_one(
    db_session, make_city, make_website, make_event
):
    city, theatre, bureau = _setup(make_city, make_website)
    _event(make_event, city, theatre, "Heist", "https://theatre/heist", is_active=False)
    _event(make_event, city, bureau, "Heist", "https://bureau/heist")

    assert _listed(db_session) == [("Heist", bureau.id)]
