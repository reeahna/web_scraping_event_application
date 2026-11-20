"""Bloomington Online: JSON-LD events with no `url` take their page anchor.

The fixture is a trimmed copy of https://bloomingtononline.com/events/ (its
JSON-LD with 40 events, and the first six days of listings). Eleven of the 40
JSON-LD events have no `url` and were rejected; each is listed on the page as
`<li id="ev...">` with the site's own `?e=<id>` link inside it.
"""

import json
from datetime import date, time
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup

from app.extraction import page_anchors
from app.extraction.dedup import dedupe_within_run
from app.extraction.normalize import normalize_candidate
from app.extraction.page_anchors import AnchorRequest, find_page_anchors
from app.extraction.patterns.jsonld import JsonLdEventPattern
from app.extraction.validate import validate_candidate
from app.schemas.extraction import SiteConfiguration
from app.services.home_town import candidate_outside_home_town
from tests.extraction_helpers import make_response, make_response_from_fixture

PAGE_URL = "https://bloomingtononline.com/events/"
CONFIG = SiteConfiguration(
    pattern_name="json_ld_event",
    listing_url=PAGE_URL,
    timezone="America/Indiana/Indianapolis",
)
BLOOMINGTON = SimpleNamespace(
    name="Bloomington", state_or_region="IN", universities=["Indiana University"]
)

# The local events that were rejected only for a missing link.
RECOVERED_LOCAL = {
    "TandemFest: Fundraiser for Tandem's Birth Center",
    "Saturday Crafternoon at the Wylie House Museum",
    "Hoppy Holiday Market at Upland Brewing",
    "Bloomington Short List: 10 Acts, 10 Minutes",
    "Public Ice Skating",
    "Holiday Public Skating",
    "Trivia Night at The Tap",
    "IU Men's Basketball vs. Jacksonville",
}
AWAY_GAMES = {
    "IU Men's Basketball vs. Kentucky (Lucas Oil Stadium)",
    "IU Football at Washington (kickoff time TBA)",
}


def _run(config: SiteConfiguration = CONFIG, fixture: str = "bloomington_online_events.html"):
    response = make_response_from_fixture(fixture, final_url=PAGE_URL)
    candidates = [
        normalize_candidate(c, config) for c in JsonLdEventPattern().extract(response, config)
    ]
    valid = [c for c in candidates if validate_candidate(c, config).is_valid]
    rejected = [c for c in candidates if not validate_candidate(c, config).is_valid]
    return candidates, valid, rejected


def _anchored(candidates):
    return [
        c for c in candidates if c.field_source_paths.get("canonical_url", "").startswith(
            "page_anchor:"
        )
    ]


def test_without_anchors_the_eleven_linkless_events_are_rejected(monkeypatch):
    monkeypatch.setattr(
        "app.extraction.patterns.jsonld.find_page_anchors", lambda *args, **kwargs: {}
    )
    candidates, valid, rejected = _run()
    assert (len(candidates), len(valid), len(rejected)) == (40, 29, 11)
    assert {c.title for c in rejected} == RECOVERED_LOCAL | AWAY_GAMES


def test_linkless_events_take_their_own_page_permalink():
    candidates, valid, rejected = _run()
    assert (len(candidates), len(valid), len(rejected)) == (40, 40, 0)

    anchored = _anchored(candidates)
    assert len(anchored) == 11
    assert {c.title for c in anchored} == RECOVERED_LOCAL | AWAY_GAMES
    for candidate in anchored:
        element_id = candidate.field_source_paths["canonical_url"].removeprefix("page_anchor:#")
        assert candidate.canonical_url == f"{PAGE_URL}?e={element_id}"
        assert candidate.canonical_url != PAGE_URL
    # No element is used for two events.
    assert len({c.canonical_url for c in anchored}) == 11


def test_hoppy_holiday_market_keeps_each_day_apart():
    candidates, _, _ = _run()
    markets = sorted(
        (c for c in candidates if c.title == "Hoppy Holiday Market at Upland Brewing"),
        key=lambda c: c.start_date,
    )
    assert [(c.start_date, c.start_time) for c in markets] == [
        (date(2026, 11, 21), time(13, 0)),
        (date(2026, 11, 22), time(13, 0)),
    ]
    assert markets[0].canonical_url == f"{PAGE_URL}?e=ev20261121ceb17a"
    assert markets[1].canonical_url == f"{PAGE_URL}?e=ev202611221e5bf7"
    assert markets[0].venue.startswith("Upland Brewing Company (At the Brewery)")


@pytest.mark.parametrize(
    ("title", "start", "start_time", "venue_start"),
    [
        (
            "TandemFest: Fundraiser for Tandem's Birth Center",
            date(2026, 11, 20),
            time(18, 0),
            "FAR Center for Contemporary Arts",
        ),
        (
            "Saturday Crafternoon at the Wylie House Museum",
            date(2026, 11, 21),
            time(11, 0),
            "Wylie House Museum",
        ),
        ("Public Ice Skating", date(2026, 11, 23), time(12, 0), "Frank Southern Ice Arena"),
        ("Holiday Public Skating", date(2026, 11, 24), time(12, 0), "Frank Southern Ice Arena"),
        ("Trivia Night at The Tap", date(2026, 11, 24), time(19, 0), "The Tap"),
        (
            "IU Men's Basketball vs. Jacksonville",
            date(2026, 11, 24),
            time(19, 0),
            "Simon Skjodt Assembly Hall",
        ),
    ],
)
def test_recovered_events_keep_title_date_time_and_venue(title, start, start_time, venue_start):
    candidates, valid, _ = _run()
    [candidate] = [c for c in valid if c.title == title]
    assert (candidate.start_date, candidate.start_time) == (start, start_time)
    assert candidate.venue.startswith(venue_start)
    assert candidate.canonical_url.startswith(f"{PAGE_URL}?e=ev")
    assert not candidate.external_source_id


def test_events_with_their_own_url_are_unchanged():
    candidates, _, _ = _run()
    cinema = next(c for c in candidates if c.title.startswith("The Ballad of Suzanne Cesaire"))
    assert cinema.canonical_url.startswith("https://events.iu.edu/")
    assert cinema.field_source_paths["canonical_url"] == "jsonpath:url"


def test_away_games_are_still_left_out_of_bloomington():
    candidates, _, _ = _run()
    by_title = {c.title: c for c in candidates}
    for title in AWAY_GAMES:
        assert candidate_outside_home_town(by_title[title], BLOOMINGTON), title
    for title in RECOVERED_LOCAL:
        assert not candidate_outside_home_town(by_title[title], BLOOMINGTON), title


def test_duplicate_detection_still_runs_on_recovered_events():
    _, valid, _ = _run()
    outcome = dedupe_within_run(valid, website_id=315, city_id=2)
    kept_titles = [c.title for c in outcome.kept]
    # Both market days survive: different links, different days.
    assert kept_titles.count("Hoppy Holiday Market at Upland Brewing") == 2
    # Two copies sharing one link still collapse, as before.
    assert outcome.duplicates_skipped >= 1
    assert len({c.canonical_url for c in _anchored(outcome.kept)}) == 11


# Small synthetic pages for the edge cases.

def _page(events: list[dict], body: str) -> str:
    graph = json.dumps({"@context": "https://schema.org", "@graph": events})
    return (
        f'<html><head><script type="application/ld+json">{graph}</script></head>'
        f"<body>{body}</body></html>"
    )


def _event(name: str, start: str, url: str | None = None) -> dict:
    event = {"@type": "Event", "name": name, "startDate": start}
    if url:
        event["url"] = url
    return event


def _extract(html: str):
    response = make_response(html, final_url="https://example.org/calendar/")
    config = SiteConfiguration(pattern_name="json_ld_event", listing_url=response.final_url)
    return [
        (c, validate_candidate(c, config))
        for c in (normalize_candidate(r, config) for r in JsonLdEventPattern().extract(
            response, config
        ))
    ]


def test_a_wrapper_holding_several_events_is_never_an_anchor():
    html = _page(
        [_event("Concert", "2026-12-01T19:00:00-05:00"), _event("Lecture", "2026-12-02")],
        '<section id="december"><h3>Concert</h3><h3>Lecture</h3></section>',
    )
    for candidate, result in _extract(html):
        assert candidate.canonical_url is None
        assert not result.is_valid


def test_an_event_missing_from_the_page_stays_rejected():
    html = _page([_event("Ghost Event", "2026-12-01")], '<li id="a1"><b>Other</b></li>')
    [(candidate, result)] = _extract(html)
    assert candidate.canonical_url is None
    assert "an event link or a source ID is required" in result.errors


def test_same_name_events_are_matched_by_start_time_not_page_order():
    html = _page(
        [
            _event("Skating", "2026-12-01T12:00:00-05:00"),
            _event("Skating", "2026-12-02T12:00:00-05:00"),
        ],
        # Listed in the opposite order to the JSON-LD.
        '<li id="dec2" data-start="1796230800"><b>Skating</b></li>'
        '<li id="dec1" data-start="1796144400"><b>Skating</b></li>',
    )
    pairs = [(c.start_date, c.canonical_url) for c, _ in _extract(html)]
    assert pairs == [
        (date(2026, 12, 1), "https://example.org/calendar/#dec1"),
        (date(2026, 12, 2), "https://example.org/calendar/#dec2"),
    ]


def test_same_name_events_without_times_need_matching_counts():
    html = _page(
        [_event("Open Mic", "2026-12-01"), _event("Open Mic", "2026-12-08")],
        '<li id="only-one"><b>Open Mic</b></li>',
    )
    assert all(c.canonical_url is None for c, _ in _extract(html))


def test_a_fragment_link_also_sets_a_source_id_so_events_stay_apart():
    html = _page(
        [_event("Concert", "2026-12-01"), _event("Lecture", "2026-12-02")],
        '<li id="c1"><b>Concert</b></li><li id="l1"><b>Lecture</b></li>',
    )
    results = _extract(html)
    assert [c.external_source_id for c, _ in results] == ["c1", "l1"]
    assert all(result.is_valid for _, result in results)
    kept = dedupe_within_run([c for c, _ in results], website_id=1, city_id=1).kept
    assert len(kept) == 2


def test_permalinks_on_other_sites_are_ignored():
    soup = BeautifulSoup(
        '<li id="e9"><b>Talk</b><a href="https://elsewhere.example/?e=e9">x</a></li>',
        "html.parser",
    )
    anchors = find_page_anchors(
        soup, "https://example.org/calendar/", [AnchorRequest(key=0, name="Talk", start=None)]
    )
    assert anchors[0].url == "https://example.org/calendar/#e9"
    assert anchors[0].is_permalink is False


def test_long_page_containers_are_not_read_as_titles():
    filler = "word " * 2000
    soup = BeautifulSoup(f'<div id="all"><p>{filler}</p><b>Talk</b></div>', "html.parser")
    assert page_anchors._short_text(soup.div) is None


def test_only_out_of_town_events_fail_the_town_check():
    """Every address on this site reads "Bloomington, IN"; the venue says where
    the event is. Exactly the two away games and the NCAA tennis in Athens,
    Ga. (two days) are out of town; all other 36 events are kept."""
    candidates, _, _ = _run()
    outside = [c for c in candidates if candidate_outside_home_town(c, BLOOMINGTON)]
    assert sorted(c.title for c in outside) == sorted(
        [*AWAY_GAMES, *["Women's Tennis vs NCAA Individual Championships"] * 2]
    )
    assert all("Bloomington" in (c.address or "") for c in outside)
