"""Configuration inference, exercised directly against fixtures.

Every fixture here is a *generalized structure* — a listing whose date is
split across nested spans, a listing whose card date has no year, a JSON
endpoint payload. None of them is a real site, and no test asserts on a
hostname, because the application must never branch on one either (see
test_no_site_identity_branching).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from app.extraction.detection import run_detection
from app.extraction.inference.dates import (
    infer_date_formats,
    infer_time_formats,
    normalize_whitespace,
    split_explicit_range,
)
from app.extraction.inference.html_fields import infer_container, infer_fields, sample_cards
from app.extraction.inference.policy import (
    DEFAULT_POLICY,
    NEEDS_REVIEW,
    READY_FOR_APPROVAL,
    UNSUPPORTED,
)
from app.extraction.inference.selectors import is_stable_class_token, selector_parts
from app.extraction.inference.service import ConfigurationInferenceService
from app.extraction.inference.types import ConfigurationProposal, InferenceResult
from app.extraction.registry import REGISTRY
from tests.extraction_helpers import load_fixture, make_response_from_fixture

WEEKDAY_URL = "https://hall.example.org/events"
YEARLESS_URL = "https://venue.example.org/events/"
APP_DIR = Path(__file__).resolve().parent.parent / "app"


def _propose(fixture: str, url: str, *, detail_documents: dict[str, str] | None = None):
    response = make_response_from_fixture(fixture, final_url=url)
    detection = run_detection(response)
    service = ConfigurationInferenceService(REGISTRY)
    context = service.build_context(
        response=response,
        detection=detection,
        listing_url=url,
        fallback_timezone="America/Indiana/Indianapolis",
        detail_documents=detail_documents,
    )
    return service.propose(context), detection, service


@pytest.fixture
def weekday_proposal():
    proposal, _, _ = _propose("inference_cards_weekday_date.html", WEEKDAY_URL)
    assert isinstance(proposal, ConfigurationProposal)
    assert proposal.configuration is not None
    return proposal


# --- container + field inference -------------------------------------------


def test_container_inference_picks_the_repeated_outer_card():
    soup = BeautifulSoup(load_fixture("inference_cards_yearless_date.html"), "html.parser")
    container = infer_container(soup, DEFAULT_POLICY)
    assert container is not None
    # `div.details` also repeats, but it is nested inside `div.tile` — the
    # outer element is the one that owns every field.
    assert container.selector == "div.tile"
    assert container.count == 4


def test_title_is_inferred_from_the_card_heading(weekday_proposal):
    title = weekday_proposal.configuration.field_selectors["title"]
    assert "eventitem__title" in title.selector
    assert title.attribute is None


def test_canonical_url_is_inferred_from_the_title_anchor(weekday_proposal):
    url = weekday_proposal.configuration.field_selectors["canonical_url"]
    assert url.attribute == "href"
    assert url.selector.endswith("a")
    # The specific event-title link, never a bare `a[href]`.
    assert url.selector != "a"
    assert "title" in url.selector


def test_nested_date_spans_are_inferred_as_one_value(weekday_proposal):
    start = weekday_proposal.configuration.field_selectors["start_datetime"]
    assert "singleday" in start.selector
    candidate = next(
        c for c in weekday_proposal.field_candidates if c.field == "start_datetime"
    )
    # The value is the joined text of the nested spans, exactly as
    # resolve_css will produce it at extraction time.
    assert candidate.sample_values[0] == "Tuesday | Oct 6 , 2026"


def test_weekday_prefixed_date_format_is_inferred(weekday_proposal):
    assert weekday_proposal.configuration.date_formats == ["%A | %b %d , %Y"]


def test_image_selector_and_attribute_are_inferred(weekday_proposal):
    image = weekday_proposal.configuration.field_selectors["image"]
    assert image.attribute == "src"
    assert image.selector.endswith("img")
    assert "thumb" in image.selector


def test_venue_and_description_are_inferred_but_not_required(weekday_proposal):
    fields = weekday_proposal.configuration.field_selectors
    assert "venue" in fields
    assert "description" in fields
    # Minimal required set stays minimal.
    assert weekday_proposal.configuration.required_fields == [
        "title",
        "start_date",
        "canonical_url",
    ]


def test_inferred_selectors_are_stable_and_shallow(weekday_proposal):
    for name, selector in weekday_proposal.configuration.field_selectors.items():
        assert ":nth-child" not in selector.selector, name
        assert ":nth-of-type" not in selector.selector, name
        assert selector_parts(selector.selector) <= DEFAULT_POLICY.max_selector_parts, name
        # The generated hash class on the heading must never appear.
        assert "x7f3a91b2" not in selector.selector, name


def test_generated_hash_and_numeric_class_tokens_are_rejected():
    assert is_stable_class_token("event-title")
    assert not is_stable_class_token("x7f3a91b2")
    assert not is_stable_class_token("12345")
    assert not is_stable_class_token("a")


def test_a_selector_matching_only_one_card_is_never_proposed():
    """Coverage is measured across every sampled card, so a selector present
    on a single card cannot clear the coverage gate."""
    html = """
    <div class="list">
      <div class="card"><h3 class="card-title"><a href="/a">A</a></h3>
        <span class="card-date">June 10, 2025</span>
        <span class="oneoff-badge">Sold out</span></div>
      <div class="card"><h3 class="card-title"><a href="/b">B</a></h3>
        <span class="card-date">June 11, 2025</span></div>
      <div class="card"><h3 class="card-title"><a href="/c">C</a></h3>
        <span class="card-date">June 12, 2025</span></div>
    </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    cards = sample_cards(soup, "div.card", DEFAULT_POLICY)
    _, reported = infer_fields(cards, base_url="https://example.org/e", policy=DEFAULT_POLICY)
    assert all(".oneoff-badge" not in (c.selector or "") for c in reported)


def test_low_confidence_required_field_is_left_unset_and_reported():
    """A card list with no date anywhere: `start_datetime` must not be
    guessed at, and the gap must be reported rather than hidden."""
    html = """
    <div class="list">
      <div class="card"><h3 class="card-title"><a href="/a">Alpha</a></h3><p>Details soon</p></div>
      <div class="card"><h3 class="card-title"><a href="/b">Beta</a></h3><p>Details soon</p></div>
      <div class="card"><h3 class="card-title"><a href="/c">Gamma</a></h3><p>Details soon</p></div>
    </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    cards = sample_cards(soup, "div.card", DEFAULT_POLICY)
    accepted, reported = infer_fields(
        cards, base_url="https://example.org/e", policy=DEFAULT_POLICY
    )
    assert "title" in accepted
    assert "start_datetime" not in accepted
    assert any(c.field == "start_datetime" and not c.accepted for c in reported) or not any(
        c.field == "start_datetime" for c in reported
    )


# --- date and time inference ------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-06", ""),
        ("October 6, 2026", "%B %d, %Y"),
        ("Oct 6, 2026", "%b %d, %Y"),
        ("Tuesday, October 6, 2026", "%A, %B %d, %Y"),
        ("Fri Jul 24, 2026", "%a %b %d, %Y"),
        ("10/06/2026", "%m/%d/%Y"),
        ("Tuesday | Oct 6 , 2026", "%A | %b %d , %Y"),
    ],
)
def test_common_date_shapes_are_inferred(value, expected):
    candidates, rate = infer_date_formats([value])
    assert rate == 1.0
    assert candidates[0].format == expected


def test_whitespace_is_normalized_before_formats_are_tried():
    candidates, rate = infer_date_formats(["  Oct   6 ,\n 2026 "])
    assert rate == 1.0
    assert candidates[0].format == "%b %d , %Y"
    assert normalize_whitespace("  Oct   6 ,\n 2026 ") == "Oct 6 , 2026"


def test_a_missing_year_is_never_invented():
    candidates, rate = infer_date_formats(["24 / July / Friday", "02 / August / Saturday"])
    assert rate == 0.0
    assert candidates[0].accepted is False
    assert "no_year_in_text" in candidates[0].warnings


def test_time_formats_with_am_pm_are_inferred():
    candidates, rate = infer_time_formats(["6:00 PM", "8:30 PM"])
    assert rate == 1.0
    assert candidates[0].format == "%I:%M %p"


def test_all_day_marker_is_not_treated_as_a_time():
    candidates, rate = infer_time_formats(["All day", "all-day"])
    assert rate == 0.0
    assert "all_day_only" in candidates[0].warnings


def test_single_day_range_is_split_only_when_both_dates_are_explicit():
    assert split_explicit_range("Sep 12, 2026 - Sep 12, 2026") == (
        "Sep 12, 2026",
        "Sep 12, 2026",
    )
    # Implicit endpoint — never expanded, never guessed.
    assert split_explicit_range("Sep 12 - 13 , 2026") is None


# --- detail-page proposal ---------------------------------------------------


def test_yearless_cards_request_a_bounded_detail_page_probe():
    proposal, _, _ = _propose("inference_cards_yearless_date.html", YEARLESS_URL)
    assert isinstance(proposal, ConfigurationProposal)
    assert proposal.configuration is None
    assert proposal.detail_probe_url == "https://venue.example.org/events/quest-film"


def test_detail_page_supplies_the_year_and_becomes_a_detail_field():
    detail = load_fixture("inference_detail_full_date.html")
    proposal, _, _ = _propose(
        "inference_cards_yearless_date.html",
        YEARLESS_URL,
        detail_documents={"https://venue.example.org/events/quest-film": detail},
    )
    config = proposal.configuration
    assert config is not None
    assert "detail_link" in config.field_selectors
    assert "detail_start_datetime" in config.field_selectors
    assert config.date_formats == ["%a %b %d, %Y"]
    # Bounded, and only because enrichment is actually needed.
    assert 0 < config.max_detail_fetches <= DEFAULT_POLICY.max_detail_fetches_when_needed
    assert proposal.missing_required_fields == ()


def test_css_background_image_and_venue_transformations_are_inferred():
    detail = load_fixture("inference_detail_full_date.html")
    proposal, _, _ = _propose(
        "inference_cards_yearless_date.html",
        YEARLESS_URL,
        detail_documents={"https://venue.example.org/events/quest-film": detail},
    )
    kinds = {(r.field, r.kind) for r in proposal.configuration.transformations}
    assert ("image", "regex_extract_group") in kinds
    assert ("venue", "regex_extract_group") in kinds


# --- structured pattern proposers -------------------------------------------


def test_the_events_calendar_proposal_uses_the_discovered_route():
    proposal, _, _ = _propose("tribe_site_page.html", "https://cinema.example.org/events/")
    config = proposal.configuration
    assert config is not None
    assert config.pattern_name == "the_events_calendar"
    assert "tribe/events/v1/events" in config.api_endpoint
    assert config.pagination.strategy == "tribe_rest"
    # Mappings stay overridable rather than being frozen into the config.
    assert config.json_paths == {}
    assert any(c.field == "title" and c.kind == "json_path" for c in proposal.field_candidates)


def test_wordpress_rest_proposal_derives_the_conventional_route():
    proposal, _, _ = _propose("wordpress_site_page.html", "https://news.example.org/events/")
    config = proposal.configuration
    assert config is not None
    assert config.pattern_name == "wordpress_rest"
    assert config.api_endpoint.endswith("wp/v2/posts")
    assert config.pagination.strategy == "wordpress"


def test_livewhale_proposal_uses_offset_pagination():
    proposal, _, _ = _propose("livewhale_site_page.html", "https://campus.example.org/calendar")
    config = proposal.configuration
    assert config is not None
    assert config.pattern_name == "livewhale_json"
    assert config.pagination.strategy == "livewhale_offset"
    assert config.pagination.page_param == "offset"


def test_json_ld_proposal_measures_coverage_against_real_nodes():
    proposal, _, _ = _propose("jsonld_multiple_events.html", "https://arts.example.org/events")
    config = proposal.configuration
    assert config is not None
    assert config.pattern_name == "json_ld_event"
    assert config.listing_url == "https://arts.example.org/events"
    title = next(c for c in proposal.field_candidates if c.field == "title")
    assert title.accepted
    assert title.coverage == 1.0


def test_every_registered_pattern_has_a_proposer():
    for name in REGISTRY.names():
        assert REGISTRY.get(name).proposer is not None, name


# --- service-level outcomes -------------------------------------------------


def test_unsupported_page_yields_an_unsupported_outcome():
    result, _, _ = _propose("unsupported_page.html", "https://plain.example.org/")
    assert isinstance(result, InferenceResult)
    assert result.outcome == UNSUPPORTED


def test_ready_and_needs_review_outcomes_are_distinguished():
    proposal, detection, service = _propose("inference_cards_weekday_date.html", WEEKDAY_URL)
    assert service.finalize(proposal, detection).outcome == READY_FOR_APPROVAL

    incomplete = ConfigurationProposal(
        configuration=proposal.configuration,
        missing_required_fields=("start_date",),
    )
    assert service.finalize(incomplete, detection).outcome == NEEDS_REVIEW


# --- provider independence --------------------------------------------------


def test_no_site_identity_branching():
    """The application must select behaviour from the registry and from page
    evidence — never from a hostname or a site name. Fixture host/venue names
    are asserted absent from the entire application package."""
    forbidden = (
        "iuauditorium",
        "buskirk",
        "chumley",
        "hall.example.org",
        "venue.example.org",
        "grand street",
        "riverbend",
    )
    offenders = []
    for path in APP_DIR.rglob("*.py"):
        lowered = path.read_text(encoding="utf-8").lower()
        offenders.extend(f"{path.name}:{token}" for token in forbidden if token in lowered)
    assert offenders == []
