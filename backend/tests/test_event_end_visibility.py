"""Events leave the public site once they have actually ended in their town.

Each test sets the clock (public_events._utcnow) to a known instant and checks
that every public route agrees: listings, counts, Today and This weekend,
search, the map, detail pages, saved events and the sitemap. Ended events stay
in the database untouched.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from app.core.permissions import REGISTERED_USER
from app.models.event import Event
from app.repositories import public_events
from app.repositories.public_events import (
    LocalDates,
    event_has_ended,
    get_public_event,
    list_public_events,
    local_today,
    this_weekend,
    town_today,
    upcoming_counts_by_city,
)
from app.services import engagement

INDY = "America/Indiana/Indianapolis"
LA = "America/Los_Angeles"

# Friday Nov 20 2026 (EST, UTC-5).
FRI = date(2026, 11, 20)


def _at(monkeypatch, moment: datetime) -> None:
    monkeypatch.setattr(public_events, "_utcnow", lambda: moment)


def _indy_local(day: date, hour: int, minute: int = 0) -> datetime:
    """A Bloomington wall-clock instant in November (EST, UTC-5) as UTC. An
    hour of 24 or more runs into the next day."""
    midnight = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return midnight + timedelta(hours=hour + 5, minutes=minute)


@pytest.fixture
def town(make_city, make_website):
    city = make_city(name="Bloomington", slug="bloomington-in", timezone=INDY)
    site = make_website(
        city, name="IU", is_active=True, approved_pattern={"pattern_name": "livewhale_json"}
    )
    return city, site


_urls = iter(range(10_000))


def _event(make_event, town, title, **values) -> Event:
    city, site = town
    return make_event(
        city, website=site, title=title, canonical_url=f"https://x/{next(_urls)}", **values
    )


def _listed(db_session, city) -> list[str]:
    events, _, _ = list_public_events(db_session, today=town_today(city), city_id=city.id)
    return [e.title for e in events]


# --- The rule itself -----------------------------------------------------------


def test_event_ending_at_2pm_today_is_hidden_after_2pm(
    monkeypatch, db_session, make_event, town
):
    city, _ = town
    _event(make_event, town, "Lunch talk", start_date=FRI, start_time=time(12),
           end_time=time(14))
    _at(monkeypatch, _indy_local(FRI, 13, 59))
    assert _listed(db_session, city) == ["Lunch talk"]
    _at(monkeypatch, _indy_local(FRI, 14, 0))
    assert _listed(db_session, city) == []


def test_event_still_happening_tonight_stays(monkeypatch, db_session, make_event, town):
    city, _ = town
    _event(make_event, town, "Late show", start_date=FRI, start_time=time(21),
           end_time=time(23, 30))
    # 11:29 PM local is already 4:29 AM Saturday in UTC.
    _at(monkeypatch, _indy_local(FRI, 23, 29))
    assert _listed(db_session, city) == ["Late show"]
    _at(monkeypatch, _indy_local(FRI, 23, 30))
    assert _listed(db_session, city) == []


def test_all_day_event_stays_until_local_midnight(monkeypatch, db_session, make_event, town):
    city, _ = town
    _event(make_event, town, "Book sale", start_date=FRI)
    _at(monkeypatch, _indy_local(FRI, 23, 59))
    assert _listed(db_session, city) == ["Book sale"]
    _at(monkeypatch, _indy_local(FRI, 24, 0))  # Saturday 00:00 local
    assert _listed(db_session, city) == []


def test_start_time_without_end_time_is_never_given_a_duration(
    monkeypatch, db_session, make_event, town
):
    city, _ = town
    _event(make_event, town, "Morning yoga", start_date=FRI, start_time=time(7))
    _at(monkeypatch, _indy_local(FRI, 23, 0))
    assert _listed(db_session, city) == ["Morning yoga"]


def test_multi_day_event_with_end_time_ends_at_that_time(
    monkeypatch, db_session, make_event, town
):
    city, _ = town
    _event(make_event, town, "Festival", start_date=date(2026, 11, 18), end_date=FRI,
           start_time=time(10), end_time=time(17))
    _at(monkeypatch, _indy_local(FRI, 16, 59))
    assert _listed(db_session, city) == ["Festival"]
    _at(monkeypatch, _indy_local(FRI, 17, 0))
    assert _listed(db_session, city) == []


def test_multi_day_event_with_only_end_date_lasts_its_whole_last_day(
    monkeypatch, db_session, make_event, town
):
    city, _ = town
    _event(make_event, town, "Exhibit", start_date=date(2026, 11, 1), end_date=FRI,
           start_time=time(10))
    _at(monkeypatch, _indy_local(FRI, 23, 59))
    assert _listed(db_session, city) == ["Exhibit"]
    _at(monkeypatch, _indy_local(FRI, 24, 0))
    assert _listed(db_session, city) == []


def test_multi_day_event_still_active_today(monkeypatch, db_session, make_event, town):
    city, _ = town
    # It ends Sunday at 5 PM; on Friday evening it is well past 5 PM, but not
    # on its last day, so it stays (and is in Today).
    _event(make_event, town, "Art fair", start_date=date(2026, 11, 18),
           end_date=date(2026, 11, 22), start_time=time(10), end_time=time(17))
    _at(monkeypatch, _indy_local(FRI, 20))
    today = town_today(city)
    events, _, _ = list_public_events(
        db_session, today=today, city_id=city.id, date_from=today, date_to=today
    )
    assert [e.title for e in events] == ["Art fair"]


def test_event_ending_at_midnight_is_treated_as_unknown_end(
    monkeypatch, db_session, make_event, town
):
    # Many sources write 00:00 when they have no time. It never ends an event
    # early: the event stays for the rest of its date.
    city, _ = town
    _event(make_event, town, "Gala", start_date=FRI, start_time=time(20), end_time=time(0))
    _at(monkeypatch, _indy_local(FRI, 23, 59))
    assert _listed(db_session, city) == ["Gala"]
    _at(monkeypatch, _indy_local(FRI, 24, 0))
    assert _listed(db_session, city) == []


def test_overnight_event_runs_until_its_end_the_next_morning(
    monkeypatch, db_session, make_event, town
):
    city, _ = town
    _event(make_event, town, "Dance night", start_date=FRI, start_time=time(22),
           end_time=time(2))
    _at(monkeypatch, _indy_local(FRI, 22, 30))
    assert _listed(db_session, city) == ["Dance night"]
    _at(monkeypatch, _indy_local(FRI, 24 + 1, 30))  # 1:30 AM Saturday
    assert _listed(db_session, city) == ["Dance night"]
    _at(monkeypatch, _indy_local(FRI, 24 + 2, 0))
    assert _listed(db_session, city) == []


def test_end_time_equal_to_start_is_not_an_end(monkeypatch, db_session, make_event, town):
    city, _ = town
    _event(make_event, town, "Open studio", start_date=FRI, start_time=time(9),
           end_time=time(9))
    _at(monkeypatch, _indy_local(FRI, 22))
    assert _listed(db_session, city) == ["Open studio"]


def test_daylight_saving_transition(monkeypatch, db_session, make_event, town):
    # Sunday Nov 1 2026 clocks fall back at 2 AM EDT. A 1 PM to 3 PM event
    # ends at 20:00 UTC (EST), not 19:00 UTC (EDT).
    city, _ = town
    sunday = date(2026, 11, 1)
    _event(make_event, town, "Matinee", start_date=sunday, start_time=time(13),
           end_time=time(15))
    _at(monkeypatch, datetime(2026, 11, 1, 19, 30, tzinfo=UTC))  # 2:30 PM EST
    assert _listed(db_session, city) == ["Matinee"]
    _at(monkeypatch, datetime(2026, 11, 1, 20, 0, tzinfo=UTC))  # 3:00 PM EST
    assert _listed(db_session, city) == []
    # And in summer time: 3 PM EDT on Jul 4 is 19:00 UTC.
    _event(make_event, town, "Parade", start_date=date(2026, 7, 4), start_time=time(13),
           end_time=time(15))
    _at(monkeypatch, datetime(2026, 7, 4, 18, 59, tzinfo=UTC))
    assert "Parade" in _listed(db_session, city)
    _at(monkeypatch, datetime(2026, 7, 4, 19, 0, tzinfo=UTC))
    assert "Parade" not in _listed(db_session, city)


def test_each_town_uses_its_own_clock(
    monkeypatch, db_session, make_city, make_website, make_event, town
):
    bloomington, _ = town
    la = make_city(name="Los Angeles", slug="los-angeles-ca", timezone=LA)
    la_site = make_website(
        la, name="LA", is_active=True, approved_pattern={"pattern_name": "x"}
    )
    for city, site in ((bloomington, town[1]), (la, la_site)):
        make_event(city, website=site, title="Concert", start_date=FRI, start_time=time(18),
                   end_time=time(20), canonical_url=f"https://x/{city.slug}")
    # 9 PM in Bloomington, 6 PM in Los Angeles.
    _at(monkeypatch, datetime(2026, 11, 21, 2, 0, tzinfo=UTC))
    counts = upcoming_counts_by_city(db_session, today=local_today(db_session))
    assert counts.get(bloomington.id) is None
    assert counts.get(la.id) == 1


def test_recurring_occurrences_end_one_by_one(monkeypatch, db_session, make_event, town):
    city, _ = town
    _event(make_event, town, "Trivia", start_date=FRI, start_time=time(19),
           is_recurrence_parent=True, recurrence_parent_id="series-1")
    for day, url in ((FRI, "occ-1"), (date(2026, 11, 27), "occ-2")):
        _event(make_event, town, "Trivia", start_date=day, start_time=time(19),
               end_time=time(21), recurrence_parent_id="series-1", occurrence_id=url)
    _at(monkeypatch, _indy_local(FRI, 20))
    events, _, _ = list_public_events(db_session, today=town_today(city), city_id=city.id)
    assert [e.start_date for e in events] == [FRI, date(2026, 11, 27)]
    _at(monkeypatch, _indy_local(FRI, 21, 30))
    events, _, _ = list_public_events(db_session, today=town_today(city), city_id=city.id)
    assert [e.start_date for e in events] == [date(2026, 11, 27)]


def test_town_on_the_utc_default_keeps_the_whole_day_rule(
    monkeypatch, db_session, make_city, make_website, make_event
):
    # Its stored times are not UTC, so a UTC clock would hide events early.
    city = make_city(name="Unset", slug="unset", timezone="UTC")
    site = make_website(city, name="S", is_active=True, approved_pattern={"pattern_name": "x"})
    make_event(city, website=site, title="Talk", start_date=FRI, start_time=time(9),
               end_time=time(10), canonical_url="https://x/u")
    _at(monkeypatch, datetime(2026, 11, 20, 15, 0, tzinfo=UTC))
    assert _listed(db_session, city) == ["Talk"]


def test_ended_events_stay_in_the_database(monkeypatch, db_session, make_event, town):
    city, _ = town
    ended = _event(make_event, town, "Done", start_date=FRI, start_time=time(9),
                   end_time=time(10))
    _at(monkeypatch, _indy_local(FRI, 12))
    assert get_public_event(db_session, ended.id, today=local_today(db_session)) is None
    db_session.expire_all()
    row = db_session.get(Event, ended.id)
    assert row.is_active is True and row.archived_at is None


def test_python_and_sql_rules_agree(monkeypatch, db_session, make_event, town):
    city, _ = town
    cases = [
        {"start_date": FRI, "start_time": time(12), "end_time": time(14)},
        {"start_date": FRI, "start_time": time(21), "end_time": time(23, 30)},
        {"start_date": FRI},
        {"start_date": FRI, "start_time": time(20), "end_time": time(0)},
        {"start_date": date(2026, 11, 19), "start_time": time(22), "end_time": time(2)},
        {"start_date": date(2026, 11, 19), "start_time": time(22), "end_time": time(23)},
        {"start_date": date(2026, 11, 18), "end_date": FRI, "end_time": time(17)},
        {"start_date": date(2026, 11, 18), "end_date": date(2026, 11, 19)},
    ]
    events = [_event(make_event, town, f"E{i}", **values) for i, values in enumerate(cases)]
    for hour in (0, 1, 13, 15, 18, 23):
        _at(monkeypatch, _indy_local(FRI, hour, 45))
        for today in (town_today(city), local_today(db_session)):
            public = {
                e.id for e in events if get_public_event(db_session, e.id, today=today)
            }
            for event in events:
                assert (event.id not in public) == event_has_ended(event, today), (
                    hour, event.title
                )


# --- Every public route agrees ---------------------------------------------------


@pytest.fixture
def friday_afternoon(monkeypatch, make_event, town):
    """3 PM Friday in Bloomington: one event ended at 2 PM, one runs tonight."""
    _event(make_event, town, "Ended Lecture", start_date=FRI, start_time=time(12),
           end_time=time(14), latitude=39.17, longitude=-86.52)
    _event(make_event, town, "Tonight Concert", start_date=FRI, start_time=time(19),
           end_time=time(21), latitude=39.17, longitude=-86.52)
    _event(make_event, town, "Saturday Market", start_date=date(2026, 11, 21),
           start_time=time(8), end_time=time(12), latitude=39.17, longitude=-86.52)
    _at(monkeypatch, _indy_local(FRI, 15))
    return town


def test_town_today_and_weekend_pages(client, friday_afternoon):
    page = client.get("/city/bloomington-in").text
    assert "Tonight Concert" in page and "Ended Lecture" not in page
    today = client.get("/city/bloomington-in/today").text
    assert "Tonight Concert" in today
    assert "Ended Lecture" not in today and "Saturday Market" not in today
    weekend = client.get("/city/bloomington-in/this-weekend").text
    assert "Saturday Market" in weekend and "Ended Lecture" not in weekend


def test_search_map_and_counts(client, db_session, friday_afternoon):
    city, _ = friday_afternoon
    found = client.get("/city/bloomington-in", params={"q": "Lecture"}).text
    assert "Ended Lecture" not in found
    points = client.get("/events/map", params={"city_id": city.id}).json()["points"]
    assert sorted(p["title"] for p in points) == ["Saturday Market", "Tonight Concert"]
    assert upcoming_counts_by_city(db_session, today=local_today(db_session)) == {city.id: 2}


def test_detail_page_and_sitemap(client, db_session, friday_afternoon):
    ended = db_session.query(Event).filter_by(title="Ended Lecture").one()
    tonight = db_session.query(Event).filter_by(title="Tonight Concert").one()
    assert client.get(f"/events/{ended.id}").status_code == 404
    assert client.get(f"/events/{tonight.id}").status_code == 200
    sitemap = client.get("/sitemap.xml").text
    assert f"/events/{ended.id}<" not in sitemap
    assert f"/events/{tonight.id}<" in sitemap


def test_saved_events_put_an_ended_event_under_past(
    db_session, make_user, friday_afternoon
):
    user = make_user(email="s@example.com", password="password12345",
                     role_name=REGISTERED_USER)
    for title in ("Ended Lecture", "Tonight Concert"):
        event = db_session.query(Event).filter_by(title=title).one()
        engagement.save_event(db_session, user_id=user.id, event_id=event.id)
    current, finished = engagement.saved_events_by_state(
        db_session, user_id=user.id, today=local_today(db_session)
    )
    assert [(i.event.title, i.state) for i in current] == [("Tonight Concert", "upcoming")]
    assert [(i.event.title, i.state) for i in finished] == [("Ended Lecture", "past")]


def test_weekend_window_still_per_town():
    dates = LocalDates({LA: FRI}, FRI, {LA: time(20)})
    saturday, sunday = this_weekend(dates)
    assert saturday.for_zone(LA) == date(2026, 11, 21)
    # Shifted dates are not "now", so they carry no clock.
    assert saturday.clock_for_zone(LA) is None
