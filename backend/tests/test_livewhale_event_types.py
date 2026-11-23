"""LiveWhale event types: a performance tagged with a talk label too.

IU's Jacobs School calendar tags some concerts with both "Lectures, talks,
seminars, and conferences" and "Performances, shows, and films", talk label
first. The first label used to become the event's category, and the
conference rule then dropped the concert as "not attendable" (The Snowy Day on
Feb 7, the Bloomington Bach Cantata Project). Pre-show talks carry the same
two labels and stay excluded, as do colloquia and symposia.
"""

from __future__ import annotations

import json

import pytest

from app.extraction.normalize import normalize_candidate
from app.extraction.patterns.livewhale_json import LiveWhalePattern
from app.schemas.extraction import SiteConfiguration
from app.services.attendability import CONFERENCE, candidate_not_attendable_reason
from tests.extraction_helpers import make_response

INDY = "America/Indiana/Indianapolis"
JACOBS = "https://events.iu.edu/live/json/events/group_id/56/paginate/500"
TALK = "Lectures, talks, seminars, and conferences"
PERFORMANCE = "Performances, shows, and films"
BOTH = [TALK, PERFORMANCE]

# Titles, dates and event types as IU's feed lists them (group_id/56).
SNOWY_DAY_FEB_7 = (
    2350601, "“The Snowy Day” – Music by Joel Thompson; "
    "Libretto by Andrea Davis Pinkney", "2027-02-07T15:00:00-05:00", BOTH,
)
BACH_CANTATAS = [
    (2429694, "Bloomington Bach Cantata Project – Brian Ford, director",
     "2027-01-31T14:30:00-05:00", BOTH),
    (2429695, "Bloomington Bach Cantata Project – Daniel R. Melamed, director",
     "2027-03-07T14:30:00-05:00", BOTH),
    (2429696, "Bloomington Bach Cantata Project – Ruben Valenzuela, director",
     "2027-04-04T14:30:00-04:00", BOTH),
]
STILL_EXCLUDED = [
    (2350621, "Opera Insights: Pre-show talk on Thompson and Pinkney’s "
     "“The Snowy Day” – Presented by Kitt Westerduin",
     "2027-02-07T14:00:00-05:00", BOTH),
    (2350624, "To The Pointe: Pre-show talk on the 2027 Spring Ballet – "
     "Hosted by Sarah Wroth", "2027-04-02T18:30:00-04:00", BOTH),
    (2399879, "Musicology Colloquium and Professional Development Series",
     "2026-12-04T12:45:00-05:00", [TALK]),
    (2411068, "33rd Annual Symposium of Music Research",
     "2027-02-19T12:00:00-05:00", [TALK]),
]


def _record(event_id, title, start, event_types, **extra):
    return {
        "id": event_id,
        "title": title,
        "url": f"https://events.iu.edu/musiciub/event/{event_id}",
        "date_iso": start,
        "timezone": INDY,
        "location_title": "Musical Arts Center",
        "event_types": event_types,
        "group_title": "Jacobs School of Music Events Calendar",
        **extra,
    }


def _candidates(records, endpoint=JACOBS):
    config = SiteConfiguration(pattern_name="livewhale_json", api_endpoint=endpoint)
    response = make_response(
        json.dumps({"data": records}), final_url=endpoint, content_type="application/json"
    )
    return [
        normalize_candidate(c, config, fallback_timezone=INDY)
        for c in LiveWhalePattern().extract(response, config)
    ]


def _one(record, endpoint=JACOBS):
    (candidate,) = _candidates([record], endpoint)
    return candidate


def test_snowy_day_on_february_7_is_kept_as_a_performance():
    candidate = _one(_record(*SNOWY_DAY_FEB_7))
    assert candidate.source_category == PERFORMANCE
    assert candidate_not_attendable_reason(candidate) is None


@pytest.mark.parametrize("record", BACH_CANTATAS, ids=lambda r: r[2][:10])
def test_bach_cantata_project_concerts_are_kept(record):
    candidate = _one(_record(*record))
    assert candidate.source_category == PERFORMANCE
    assert candidate_not_attendable_reason(candidate) is None


@pytest.mark.parametrize("record", STILL_EXCLUDED, ids=lambda r: str(r[0]))
def test_talks_colloquia_and_symposia_stay_excluded(record):
    candidate = _one(_record(*record))
    assert candidate.source_category == TALK
    # Still left out (a colloquium counts as internal faculty business).
    assert candidate_not_attendable_reason(candidate) is not None


def test_one_feed_keeps_the_concerts_and_drops_the_talks():
    records = [_record(*r) for r in [SNOWY_DAY_FEB_7, *BACH_CANTATAS, *STILL_EXCLUDED]]
    kept = {c.title for c in _candidates(records) if not candidate_not_attendable_reason(c)}
    assert kept == {SNOWY_DAY_FEB_7[1], *(r[1] for r in BACH_CANTATAS)}


def test_a_performance_listed_alone_or_first_is_unchanged():
    alone = _one(_record(2350600, "The Snowy Day", "2027-02-06T17:00:00-05:00",
                         [PERFORMANCE]))
    first = _one(_record(1, "Concert", "2027-02-06T17:00:00-05:00",
                         [PERFORMANCE, TALK]))
    mixed = _one(_record(2, "Holiday Open House", "2027-02-06T17:00:00-05:00",
                         ["Art, music, films, and performances", "Community events"]))
    assert alone.source_category == PERFORMANCE
    assert first.source_category == PERFORMANCE
    assert mixed.source_category == "Art, music, films, and performances"


def test_a_talk_label_with_no_performance_label_is_unchanged():
    candidate = _one(_record(3, "Faculty Panel", "2027-02-06T17:00:00-05:00",
                             [TALK, "Community events"]))
    assert candidate.source_category == TALK


def test_a_group_name_still_comes_first():
    candidate = _one(_record(*SNOWY_DAY_FEB_7, groups=[{"name": "Opera and Ballet"}]))
    assert candidate.source_category == "Opera and Ballet"


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://events.iu.edu/live/json/events/paginate/500",  # IU Calendar
        "https://events.iu.edu/live/json/events/group_id/10/paginate/500",
        "https://calendar.example.edu/live/json/events",  # another LiveWhale site
    ],
)
def test_other_livewhale_sources_with_the_same_types(endpoint):
    concert = _one(_record(*BACH_CANTATAS[0]), endpoint)
    talk = _one(_record(*STILL_EXCLUDED[0]), endpoint)
    assert candidate_not_attendable_reason(concert) is None
    assert candidate_not_attendable_reason(talk) == CONFERENCE


def test_event_types_given_as_objects():
    candidate = _one(_record(*BACH_CANTATAS[1][:3], [{"name": TALK}, {"name": PERFORMANCE}]))
    assert candidate.source_category == PERFORMANCE
