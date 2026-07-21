"""Regression tests for the two Phase 8B/8C failures found in verification.

1. A second Website was created for a source that already existed.
2. Automatic date inference produced no start-date selector, so every
   previewed candidate was rejected.

Both fixtures below are generalized structures. No hostname or site name from
any real source appears in them or in the assertions.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.core.onboarding_jobs import DUPLICATE, NEEDS_REVIEW, READY_FOR_APPROVAL
from app.core.url_canonical import canonical_origin, canonical_url, same_resource
from app.extraction.detection import run_detection
from app.extraction.inference.dates import infer_date_formats
from app.extraction.inference.html_fields import infer_container, infer_fields, sample_cards
from app.extraction.inference.policy import DEFAULT_POLICY
from app.extraction.inference.service import ConfigurationInferenceService
from app.extraction.registry import REGISTRY
from app.models.website import Website
from app.repositories.onboarding import find_website_match
from app.services.bulk_onboarding import create_batch_from_submission, process_batch
from app.services.onboarding_submission import SubmissionLimits, parse_url_lines
from tests.extraction_helpers import load_fixture, make_response_from_fixture, patched_http_fetch

LIMITS = SubmissionLimits(
    max_urls=50, max_csv_rows=50, max_csv_bytes=100_000, max_url_length=2000
)
LISTING_URL = "https://venue.example.org/events"


# --- Part B: canonical URL comparison ---------------------------------------


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("https://venue.example.org/events", "https://venue.example.org/events"),
        ("https://venue.example.org/events", "https://venue.example.org/events/"),
        ("https://venue.example.org/events", "https://VENUE.Example.ORG/events"),
        ("https://venue.example.org/events", "https://venue.example.org/events#agenda"),
        ("https://venue.example.org/events", "https://venue.example.org:443/events"),
        ("http://venue.example.org/events", "http://venue.example.org:80/events"),
        ("https://venue.example.org", "https://venue.example.org/"),
        ("https://venue.example.org/e?b=2&a=1", "https://venue.example.org/e?a=1&b=2"),
    ],
)
def test_canonical_form_treats_these_as_one_resource(left, right):
    assert same_resource(left, right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("https://venue.example.org/events", "https://venue.example.org/calendar"),
        ("https://venue.example.org/theatre/events", "https://venue.example.org/music/events"),
        ("https://venue.example.org/events", "https://other.example.org/events"),
        ("https://venue.example.org/events", "http://venue.example.org/events"),
        ("https://venue.example.org/events?a=1", "https://venue.example.org/events?a=2"),
    ],
)
def test_canonical_form_keeps_these_distinct(left, right):
    assert not same_resource(left, right)


def test_unparseable_urls_never_match_each_other():
    assert not same_resource("", "")
    assert not same_resource("not a url", "also not a url")


def test_canonical_origin_reduces_the_path():
    assert canonical_origin("https://venue.example.org/events/detail/x") == (
        "https://venue.example.org/"
    )
    assert canonical_url("https://venue.example.org") == "https://venue.example.org/"


# --- Part B: existing-website matching --------------------------------------


@pytest.fixture
def city(make_city):
    return make_city(name="Correction City", slug="correction-city", timezone="UTC")


@pytest.fixture
def approved_website(db_session, city, make_website):
    """An already-approved, active website — exactly the state the duplicate
    was created alongside."""
    website = make_website(
        city,
        name="Existing Venue",
        base_url="https://venue.example.org",
        approved_pattern={"pattern_name": "generic_html_cards", "listing_url": LISTING_URL},
        is_active=True,
    )
    website.event_listing_url = LISTING_URL
    website.onboarding_status = "active"
    db_session.commit()
    return website


@pytest.mark.parametrize(
    "submitted",
    [
        LISTING_URL,
        f"{LISTING_URL}/",
        "https://VENUE.example.org/events",
        "https://venue.example.org/events#calendar",
        "https://venue.example.org",
    ],
)
def test_an_existing_website_is_matched_however_the_url_is_written(
    db_session, approved_website, submitted
):
    match = find_website_match(db_session, submitted)
    assert match is not None
    assert match.website.id == approved_website.id
    assert match.reason


def test_a_different_event_path_does_not_falsely_match(db_session, approved_website):
    assert find_website_match(db_session, "https://venue.example.org/calendar") is None
    assert find_website_match(db_session, "https://venue.example.org/music/events") is None


def test_a_page_under_an_origin_only_website_matches_that_website(db_session, city, make_website):
    website = make_website(city, name="Origin Only", base_url="https://origin.example.org")
    match = find_website_match(db_session, "https://origin.example.org/whats-on")
    assert match is not None and match.website.id == website.id


def test_a_page_does_not_match_a_website_that_already_has_its_own_listing_url(
    db_session, approved_website
):
    # `approved_website` has an explicit listing URL, so an unrelated page on
    # the same host is a different source, not the same one.
    assert find_website_match(db_session, "https://venue.example.org/gallery/exhibits") is None


# --- Part B: end-to-end, no second Website ----------------------------------


def _handler(routes: dict[str, str]):
    def handler(request: httpx.Request) -> httpx.Response:
        body = routes.get(str(request.url))
        if body is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    return handler


def _run_batch(db_session, city, url, *, redetect=False, routes=None,
               fixture="inference_cards_slash_year_date.html"):
    parsed = parse_url_lines(url, LIMITS)
    batch = create_batch_from_submission(
        db_session,
        parsed,
        submitted_by_user_id=None,
        default_city_id=city.id,
        default_timezone=None,
        redetect_existing=redetect,
        source_kind="single",
        correlation_id="correction-test",
    )
    served = routes if routes is not None else {url: load_fixture(fixture)}
    with patched_http_fetch(_handler(served)):
        asyncio.run(process_batch(db_session, batch, limit=5))
    db_session.refresh(batch)
    return batch


def test_submitting_an_existing_source_creates_no_second_website(
    db_session, city, approved_website
):
    before = db_session.query(Website).count()
    batch = _run_batch(db_session, city, LISTING_URL)
    job = batch.jobs[0]

    assert job.status == DUPLICATE
    assert job.duplicate_of_website_id == approved_website.id
    assert db_session.query(Website).count() == before


def test_matching_preserves_approved_configuration_and_active_state(
    db_session, city, approved_website
):
    approved_before = dict(approved_website.approved_pattern)
    version_before = approved_website.active_configuration_version

    _run_batch(db_session, city, LISTING_URL)
    db_session.refresh(approved_website)

    assert approved_website.approved_pattern == approved_before
    assert approved_website.active_configuration_version == version_before
    assert approved_website.onboarding_status == "active"
    assert approved_website.is_active is True
    assert approved_website.configuration is None  # detection was not re-run


def test_redetection_is_opt_in_and_only_writes_a_draft(db_session, city, approved_website):
    approved_before = dict(approved_website.approved_pattern)
    before = db_session.query(Website).count()

    batch = _run_batch(db_session, city, LISTING_URL, redetect=True)
    db_session.refresh(approved_website)

    assert db_session.query(Website).count() == before
    assert approved_website.approved_pattern == approved_before
    assert approved_website.is_active is True
    assert approved_website.configuration is not None
    assert batch.jobs[0].website_id == approved_website.id


# --- Part D: date inference regressions -------------------------------------


def _propose(fixture: str, url: str = LISTING_URL):
    response = make_response_from_fixture(fixture, final_url=url)
    detection = run_detection(response)
    service = ConfigurationInferenceService(REGISTRY)
    context = service.build_context(
        response=response, detection=detection, listing_url=url, fallback_timezone="UTC"
    )
    return service.propose(context)


def _cards(fixture: str):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(load_fixture(fixture), "html.parser")
    container = infer_container(soup, DEFAULT_POLICY)
    assert container is not None
    return sample_cards(soup, container.selector, DEFAULT_POLICY)


def test_nested_date_parts_are_read_from_the_parent_container():
    """The date lives in a parent whose children each hold one component. The
    parent must be proposed, not skipped in favour of a leaf."""
    accepted, _ = infer_fields(
        _cards("inference_cards_nested_date_parts.html"),
        base_url=LISTING_URL,
        policy=DEFAULT_POLICY,
    )
    assert "start_datetime" in accepted
    candidate = accepted["start_datetime"]
    assert "date" in candidate.selector
    assert candidate.sample_values[0] == "Tuesday | Oct 6 , 2026"


def test_weekday_prefixed_punctuation_spaced_date_is_inferred_end_to_end():
    proposal = _propose("inference_cards_nested_date_parts.html")
    assert proposal.configuration is not None
    assert "start_datetime" in proposal.configuration.field_selectors
    assert proposal.configuration.date_formats == ["%A | %b %d , %Y"]
    assert proposal.missing_required_fields == ()


def test_a_separator_before_the_year_is_a_supported_date_shape():
    candidates, rate = infer_date_formats(["Oct 14 / 2026", "Dec 05 / 2026", "Jan 27 / 2027"])
    assert rate == 1.0
    assert candidates[0].format == "%b %d / %Y"


def test_slash_year_cards_produce_a_date_selector_and_valid_coverage():
    proposal = _propose("inference_cards_slash_year_date.html")
    config = proposal.configuration
    assert config is not None
    assert "start_datetime" in config.field_selectors
    assert config.date_formats == ["%b %d / %Y"]
    assert proposal.missing_required_fields == ()
    # The one implicit-endpoint range card is not guessed at; the other four
    # parse, which is what keeps the source usable.
    candidate = next(c for c in proposal.field_candidates if c.field == "start_datetime")
    assert candidate.parse_success_rate == pytest.approx(0.8)


def test_a_machine_readable_datetime_attribute_is_preferred_over_rendered_text():
    accepted, _ = infer_fields(
        _cards("inference_cards_time_element.html"), base_url=LISTING_URL, policy=DEFAULT_POLICY
    )
    candidate = accepted["start_datetime"]
    assert candidate.attribute == "datetime"
    assert candidate.sample_values[0] == "2026-10-06"
    assert any("machine-readable" in item for item in candidate.evidence)


def test_rejected_date_candidates_are_still_reported_as_evidence():
    """A date that cannot be parsed must leave a trace explaining why, not
    vanish silently."""
    from bs4 import BeautifulSoup

    html = """
    <div class="list">
      <div class="card"><span class="date">Coming soon</span>
        <h3 class="card-title"><a href="/a">Alpha Production</a></h3></div>
      <div class="card"><span class="date">Dates TBA</span>
        <h3 class="card-title"><a href="/b">Beta Production</a></h3></div>
      <div class="card"><span class="date">Watch this space</span>
        <h3 class="card-title"><a href="/c">Gamma Production</a></h3></div>
    </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    cards = sample_cards(soup, "div.card", DEFAULT_POLICY)
    accepted, reported = infer_fields(cards, base_url=LISTING_URL, policy=DEFAULT_POLICY)
    assert "start_datetime" not in accepted
    assert "title" in accepted  # the rest of the card is still usable


# --- Required-field alias contract ------------------------------------------


def test_the_date_field_naming_contract_holds_end_to_end():
    """Two names exist by design and must not drift:

    * `start_datetime` — the *raw extracted* field. What a pattern resolves,
      what `field_selectors` is keyed by, what the normalizer parses. The
      detail-page variant is `detail_start_datetime`, which
      app.extraction.detail_pages merges back onto `start_datetime`.
    * `start_date` — the *typed, validated* field on EventCandidate. What
      `required_fields`, the validator, preview-quality coverage and the
      proposals' `missing_required_fields` all speak.
    """
    from app.extraction.detail_pages import _DETAIL_FIELD_PREFIX
    from app.extraction.inference.base import DEFAULT_REQUIRED_FIELDS
    from app.extraction.inference.html_fields import ROLES
    from app.extraction.inference.quality import _FIELD_ACCESSORS
    from app.extraction.normalize import normalize_candidate
    from app.extraction.patterns.static_html import StaticHtmlCardsPattern
    from app.extraction.validate import _ALWAYS_REQUIRED
    from app.schemas.extraction import SiteConfiguration

    # Raw side speaks start_datetime.
    assert "start_datetime" in ROLES
    assert "start_date" not in ROLES
    assert _DETAIL_FIELD_PREFIX + "start_datetime" == "detail_start_datetime"

    # The pattern really does emit `start_datetime` into `raw`, and the
    # normalizer really does turn it into a typed `start_date`.
    response = make_response_from_fixture(
        "inference_cards_nested_date_parts.html", final_url=LISTING_URL
    )
    config = SiteConfiguration(
        pattern_name="generic_html_cards",
        listing_url=LISTING_URL,
        event_container_selector="div.event-card",
        field_selectors={"start_datetime": {"kind": "css", "selector": ".date"}},
        date_formats=["%A | %b %d , %Y"],
    )
    candidate = StaticHtmlCardsPattern().extract(response, config)[0]
    assert "start_datetime" in candidate.raw
    assert "start_date" not in candidate.raw
    assert normalize_candidate(candidate, config).start_date is not None

    # Typed/validated side speaks start_date.
    assert "start_date" in _ALWAYS_REQUIRED
    assert "start_date" in _FIELD_ACCESSORS
    assert "start_date" in DEFAULT_REQUIRED_FIELDS
    assert "start_date" in SiteConfiguration.model_fields["required_fields"].default
    assert "start_datetime" not in _FIELD_ACCESSORS


def test_a_proposal_missing_the_date_never_reports_itself_ready():
    from app.extraction.inference.policy import READY_FOR_APPROVAL as INFERENCE_READY
    from app.extraction.inference.types import ConfigurationProposal

    proposal = _propose("inference_cards_slash_year_date.html")
    service = ConfigurationInferenceService(REGISTRY)
    response = make_response_from_fixture(
        "inference_cards_slash_year_date.html", final_url=LISTING_URL
    )
    detection = run_detection(response)

    assert service.finalize(proposal, detection).outcome == INFERENCE_READY
    without_date = ConfigurationProposal(
        configuration=proposal.configuration, missing_required_fields=("start_date",)
    )
    assert service.finalize(without_date, detection).outcome != INFERENCE_READY


# --- Part D: fail-closed behaviour ------------------------------------------


def test_a_source_with_no_inferable_date_is_needs_review_not_ready(db_session, city):
    """No date selector -> the required field is reported missing, the outcome
    is needs_review, and nothing is eligible for approval."""
    url = "https://nodate.example.org/events"
    # The listing carries month/day but no year, and the detail page it links
    # to has no date either — so no year can be found anywhere without
    # inventing one.
    batch = _run_batch(
        db_session,
        city,
        url,
        routes={
            url: load_fixture("inference_cards_no_date.html"),
            "https://nodate.example.org/events/announcement-one": load_fixture(
                "inference_detail_no_date.html"
            ),
        },
    )
    job = batch.jobs[0]

    assert job.status == NEEDS_REVIEW
    assert job.status != READY_FOR_APPROVAL
    assert job.events_valid == 0
    website = db_session.get(Website, job.website_id)
    inference = website.proposed_pattern["inference"]
    assert "start_date" in inference["inference"]["missing_required_fields"]
    assert any("start_date" in reason for reason in inference["blocking_reasons"])
    assert website.approved_pattern is None
