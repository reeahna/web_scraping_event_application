"""Moss Arts Center (Blacksburg) regression tests.

The fixture is a trimmed copy of https://artscenter.vt.edu/all-events.html
(69 cards, 24 distinct listings). Its saved configuration (v2) read each date
with `[A-Z]{3,4}\\.? \\d{1,2}, \\d{4}`: "MARCH 3, 2027" became "ARCH 3, 2027" and
"WED., SEPT. 9-SAT., NOV. 21, 2026" started on Nov 21. MOSS_CONFIG is the
corrected configuration; LEGACY_CONFIG is v2, kept to show the failure.
"""

from datetime import date, time

import pytest
from bs4 import BeautifulSoup

from app.extraction.dedup import dedupe_within_run
from app.extraction.normalize import normalize_candidate
from app.extraction.patterns.static_html import StaticHtmlCardsPattern
from app.extraction.selectors import resolve_css, visible_text
from app.extraction.transform import parse_date_value
from app.extraction.validate import validate_candidate
from app.schemas.extraction import SiteConfiguration
from tests.extraction_helpers import make_response, make_response_from_fixture

LISTING_URL = "https://artscenter.vt.edu/all-events.html"
FIXTURE = "moss_arts_center_all_events.html"

_SELECTORS = {
    "title": {"kind": "css", "selector": "a.vt-list-item-title-link"},
    "canonical_url": {
        "kind": "css",
        "selector": "a.vt-list-item-title-link",
        "attribute": "href",
    },
    "start_datetime": {"kind": "css", "selector": "p.vt-list-item-description"},
    "start_time": {"kind": "css", "selector": "p.vt-list-item-description"},
    "description": {"kind": "css", "selector": "p.vt-list-item-description"},
    "image": {"kind": "css", "selector": "img.vt-list-item-img", "attribute": "src"},
}

_BASE = {
    "pattern_name": "generic_html_cards",
    "listing_url": LISTING_URL,
    "timezone": "America/New_York",
    "event_container_selector": "li.item",
    "max_detail_fetches": 0,
    "field_selectors": _SELECTORS,
    "date_formats": ["%b %d, %Y"],
    "time_formats": ["%I:%M %p", "%I %p"],
}

_TIME_RULE = {
    "field": "start_time",
    "kind": "regex_extract_group",
    "params": {"pattern": r"\d{4}, (\d{1,2}(?::\d\d)? [AP]M)", "group": 1},
}

# Corrected configuration: the date is everything before the first year (so a
# range keeps both sides and the leading weekday), read by the shared parsers.
MOSS_CONFIG = SiteConfiguration.model_validate(
    {
        **_BASE,
        "transformations": [
            {"field": "title", "kind": "collapse_whitespace", "params": {}},
            {
                "field": "start_datetime",
                "kind": "regex_extract_group",
                "params": {"pattern": r"^([^:]*?\d{4})", "group": 1},
            },
            _TIME_RULE,
        ],
    }
)

# The configuration saved in production as v2 (approved Nov 20, 2026).
LEGACY_CONFIG = SiteConfiguration.model_validate(
    {
        **_BASE,
        "transformations": [
            {"field": "title", "kind": "collapse_whitespace", "params": {}},
            {
                "field": "start_datetime",
                "kind": "regex_extract_group",
                "params": {"pattern": r"[A-Z]{3,4}\.? \d{1,2}, \d{4}", "group": 0},
            },
            {
                "field": "start_datetime",
                "kind": "literal_replace",
                "params": {"find": ".", "replace": ""},
            },
            {
                "field": "start_datetime",
                "kind": "literal_replace",
                "params": {"find": "SEPT", "replace": "SEP"},
            },
            _TIME_RULE,
        ],
    }
)

# The eight dated performances v2 rejected.
RECOVERED = {
    "National Geographic Live": (date(2027, 3, 3), time(19, 30)),
    '"Hadestown"': (date(2027, 3, 15), time(19, 30)),
    '"An Evening with Sona Jobarteh"': (date(2027, 3, 24), time(19, 30)),
    "School Matinee | Sona Jobarteh": (date(2027, 3, 25), None),
    "Emanuel Ax, piano, and Yo-Yo Ma, cello": (date(2027, 3, 29), time(19, 30)),
    "tenThing Brass Ensemble": (date(2027, 4, 2), time(19, 30)),
    'School Matinee | School of Performing Arts: "A Midsummer Night\'s Dream"': (
        date(2027, 4, 23),
        time(10, 0),
    ),
}
ITRAAB_APRIL_URL = "https://artscenter.vt.edu/experiences/itraab-april-2027.html"

# Undated or long-running items: 9 cards each, all correctly rejected.
STILL_REJECTED = {
    '"Beyond the Frame"',
    "Turner Street Mural: Michael Scoggins",
    "Grand Lobby Mural: Brian Dettmer",
}


def _run(config: SiteConfiguration):
    response = make_response_from_fixture(FIXTURE, final_url=LISTING_URL)
    candidates = [
        normalize_candidate(c, config) for c in StaticHtmlCardsPattern().extract(response, config)
    ]
    valid = [c for c in candidates if validate_candidate(c, config).is_valid]
    rejected = [c for c in candidates if not validate_candidate(c, config).is_valid]
    return candidates, valid, rejected


def _by_url(candidates):
    out = {}
    for candidate in candidates:
        out.setdefault(candidate.canonical_url, candidate)
    return out


def test_legacy_configuration_reproduces_the_production_preview():
    candidates, valid, rejected = _run(LEGACY_CONFIG)
    assert (len(candidates), len(valid), len(rejected)) == (69, 26, 43)


def test_corrected_configuration_recovers_the_eight_performances():
    candidates, valid, rejected = _run(MOSS_CONFIG)
    assert (len(candidates), len(valid), len(rejected)) == (69, 42, 27)

    by_title = {c.title: c for c in valid}
    for title, (start, start_time) in RECOVERED.items():
        assert by_title[title].start_date == start, title
        assert by_title[title].start_time == start_time, title
    itraab = _by_url(valid)[ITRAAB_APRIL_URL]
    assert itraab.title == "Itraab Music Ensemble Concert"
    assert (itraab.start_date, itraab.start_time) == (date(2027, 4, 26), time(19, 30))

    legacy_valid_urls = {c.canonical_url for c in _run(LEGACY_CONFIG)[1]}
    recovered_urls = {c.canonical_url for c in valid} - legacy_valid_urls
    assert len(recovered_urls) == 8


def test_undated_and_long_running_items_stay_rejected():
    _, valid, rejected = _run(MOSS_CONFIG)
    assert len(rejected) == 27
    assert {c.title for c in rejected} == STILL_REJECTED
    assert all(c.start_date is None for c in rejected)
    assert not {c.title for c in valid} & STILL_REJECTED


@pytest.mark.parametrize(
    ("url", "start", "end"),
    [
        (
            "https://artscenter.vt.edu/exhibitions/guadalupe-maravilla.html",
            date(2026, 9, 9),
            date(2026, 11, 21),
        ),
        (
            "https://artscenter.vt.edu/exhibitions/young-artists-radford-high-school.html",
            date(2026, 12, 3),
            date(2026, 12, 19),
        ),
        (
            "https://artscenter.vt.edu/performances/alvin-ailey-2027.html",
            date(2027, 2, 23),
            date(2027, 2, 24),
        ),
    ],
)
def test_date_ranges_keep_their_real_start_and_end(url, start, end):
    _, valid, _ = _run(MOSS_CONFIG)
    candidate = _by_url(valid)[url]
    assert (candidate.start_date, candidate.end_date) == (start, end)


def test_single_dates_have_no_end_date():
    _, valid, _ = _run(MOSS_CONFIG)
    hadestown = _by_url(valid)["https://artscenter.vt.edu/performances/hadestown.html"]
    assert hadestown.end_date is None


def test_titles_leave_out_screen_reader_only_text():
    candidates, _, _ = _run(MOSS_CONFIG)
    assert not [c.title for c in candidates if "article" in c.title.lower()]
    assert any(c.title == "Guadalupe Maravilla" for c in candidates)
    # Visible text that happens to contain the same words is kept.
    assert any(c.title == '"Young Artists: Against the Grain"' for c in candidates)


def test_repeated_cards_collapse_to_one_event_each():
    _, valid, _ = _run(MOSS_CONFIG)
    outcome = dedupe_within_run(valid, website_id=318, city_id=1)
    assert len(outcome.kept) == 21
    assert outcome.duplicates_skipped == 21
    assert len({c.canonical_url for c in outcome.kept}) == 21


def test_missing_description_leaves_the_card_rejected_not_crashing():
    html = (
        '<ul><li class="item"><a class="vt-list-item-title-link" href="/x.html">'
        "No date here</a></li></ul>"
    )
    response = make_response(html, final_url=LISTING_URL)
    [candidate] = StaticHtmlCardsPattern().extract(response, MOSS_CONFIG)
    candidate = normalize_candidate(candidate, MOSS_CONFIG)
    assert candidate.title == "No date here"
    assert candidate.start_date is None
    result = validate_candidate(candidate, MOSS_CONFIG)
    assert "a parseable start date is required" in result.errors


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("MARCH 3, 2027", date(2027, 3, 3)),
        ("APRIL 26, 2027", date(2027, 4, 26)),
        ("JUNE 4, 2027", date(2027, 6, 4)),
        ("JULY 9, 2027", date(2027, 7, 9)),
        ("SEPTEMBER 9, 2026", date(2026, 9, 9)),
        ("DEC. 3, 2026", date(2026, 12, 3)),
        ("SEPT. 9, 2026", date(2026, 9, 9)),
        ("Feb 12, 2027", date(2027, 2, 12)),
        ("MAY 1, 2027", date(2027, 5, 1)),
        # The v2 rule's truncated output, and other values that are not dates.
        ("ARCH 3, 2027", None),
        ("PRIL 26, 2027", None),
        ("FEB. 30, 2027", None),
        ("AUGUST 2026", None),
        ("THROUGH 2028", None),
        ("", None),
    ],
)
def test_abbreviated_format_also_reads_full_month_names(value, expected):
    assert parse_date_value(value, ["%b %d, %Y"]) == expected


def test_full_month_format_also_reads_abbreviations():
    assert parse_date_value("Mar 3, 2027", ["%B %d, %Y"]) == date(2027, 3, 3)


def test_visible_text_falls_back_when_everything_is_screen_reader_only():
    soup = BeautifulSoup('<a><span class="sr-only">Read more</span></a>', "html.parser")
    assert visible_text(soup.a) == "Read more"


def test_visible_only_does_not_touch_attributes_or_other_fields():
    soup = BeautifulSoup(
        '<div><a href="/e.html">Show <span class="visually-hidden">, article</span></a></div>',
        "html.parser",
    )
    card = soup.div
    assert resolve_css(card, "a", visible_only=True).value == "Show"
    assert resolve_css(card, "a").value == "Show , article"
    assert resolve_css(card, "a", "href", visible_only=True).value == "/e.html"
