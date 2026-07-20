import pytest
from pydantic import ValidationError

from app.extraction.detail_pages import enrich_with_detail_pages
from app.extraction.normalize import normalize_candidate
from app.extraction.patterns.static_html import StaticHtmlCardsPattern
from app.extraction.selectors import InvalidSelectorError, validate_css_selector
from app.extraction.validate import validate_candidate
from app.schemas.extraction import FieldSelectorConfig, SiteConfiguration
from tests.extraction_helpers import make_response_from_fixture

FIELD_SELECTORS = {
    "title": {"kind": "css", "selector": ".event-title a"},
    "canonical_url": {"kind": "css", "selector": ".event-title a", "attribute": "href"},
    "description": {"kind": "css", "selector": ".event-description"},
    "start_datetime": {"kind": "css", "selector": ".event-date"},
    "start_time": {"kind": "css", "selector": ".event-time"},
    "venue": {"kind": "css", "selector": ".event-venue"},
    "image": {"kind": "css", "selector": ".event-image", "attribute": "src"},
    "detail_link": {"kind": "css", "selector": ".event-detail-link", "attribute": "href"},
}

CONFIG = SiteConfiguration(
    pattern_name="generic_html_cards",
    listing_url="https://example.com/events",
    event_container_selector=".event-card",
    field_selectors=FIELD_SELECTORS,
    date_formats=["%B %d, %Y"],
    time_formats=["%I:%M %p"],
)


def test_static_cards_extracted_with_relative_and_absolute_urls():
    response = make_response_from_fixture(
        "static_html_cards.html", final_url="https://example.com/events"
    )
    candidates = StaticHtmlCardsPattern().extract(response, CONFIG)
    assert len(candidates) == 3
    assert candidates[0].raw["canonical_url"] == "https://example.com/events/food-fest"
    assert candidates[1].raw["canonical_url"] == "https://example.com/events/art-walk"


def test_attribute_extraction_for_image_and_link():
    response = make_response_from_fixture(
        "static_html_cards.html", final_url="https://example.com/events"
    )
    candidate = StaticHtmlCardsPattern().extract(response, CONFIG)[0]
    assert candidate.raw["image"] == "https://example.com/images/food-fest.jpg"
    assert candidate.field_source_paths["image"] == "css:.event-image@src"


def test_missing_selector_values_produce_warnings_not_crashes():
    response = make_response_from_fixture(
        "static_html_cards.html", final_url="https://example.com/events"
    )
    candidates = StaticHtmlCardsPattern().extract(response, CONFIG)
    # Third card has no description/time/image/detail_link.
    third = candidates[2]
    assert third.raw["description"] is None
    assert any("description" in w for w in third.warnings)


def test_normalized_dates_and_times_parsed_from_configured_formats():
    response = make_response_from_fixture(
        "static_html_cards.html", final_url="https://example.com/events"
    )
    candidate = StaticHtmlCardsPattern().extract(response, CONFIG)[0]
    normalized = normalize_candidate(candidate, CONFIG)
    assert normalized.start_date is not None
    assert normalized.start_date.isoformat() == "2025-06-10"
    assert normalized.start_time is not None
    assert normalized.start_time.hour == 18
    result = validate_candidate(normalized, CONFIG)
    assert result.is_valid, result.errors


@pytest.mark.asyncio
async def test_detail_page_fetch_enriches_description():
    class FakeFetch:
        async def fetch(self, request, config):
            from tests.extraction_helpers import make_response_from_fixture as load

            return load("static_html_detail_page.html", final_url=request.url)

    detail_config = SiteConfiguration(
        pattern_name="generic_html_cards",
        listing_url="https://example.com/events",
        event_container_selector=".event-card",
        field_selectors={
            **FIELD_SELECTORS,
            "detail_description": {"kind": "css", "selector": ".full-description"},
        },
    )
    response = make_response_from_fixture(
        "static_html_cards.html", final_url="https://example.com/events"
    )
    candidates = StaticHtmlCardsPattern().extract(response, detail_config)
    enriched = await enrich_with_detail_pages(candidates, FakeFetch(), detail_config)
    # "detail_description" in field_selectors enriches the base "description"
    # field from the detail page's fuller content.
    assert "only available on the detail page" in enriched[0].raw["description"]
    assert enriched[0].field_source_paths["description"].startswith("detail:")


@pytest.mark.asyncio
async def test_detail_page_fetch_deduplicates_shared_urls():
    """Two cards can legitimately share one detail_link (e.g. multiple
    showtimes of the same production). The detail page must be fetched at
    most once per run — never once per card — and the skipped duplicate
    call must still leave a warning trail (see app.extraction.detail_pages)."""
    call_count = 0

    class CountingFetch:
        async def fetch(self, request, config):
            nonlocal call_count
            call_count += 1
            return make_response_from_fixture("static_html_detail_page.html", final_url=request.url)

    detail_config = SiteConfiguration(
        pattern_name="generic_html_cards",
        listing_url="https://example.com/events",
        event_container_selector=".event-card",
        field_selectors={
            **FIELD_SELECTORS,
            "detail_description": {"kind": "css", "selector": ".full-description"},
        },
    )
    response = make_response_from_fixture(
        "static_html_cards_shared_detail.html", final_url="https://example.com/events"
    )
    candidates = StaticHtmlCardsPattern().extract(response, detail_config)
    assert len(candidates) == 3

    enriched = await enrich_with_detail_pages(candidates, CountingFetch(), detail_config)

    # Two cards share https://example.com/events/food-fest; the third
    # (art-walk) is a distinct URL — exactly two real fetches, not three.
    assert call_count == 2

    first, second, third = enriched
    assert "only available on the detail page" in first.raw["description"]
    assert "only available on the detail page" in second.raw["description"]
    assert "only available on the detail page" in third.raw["description"]
    # The second card reused the first's cached response rather than
    # triggering its own fetch — recorded as a warning, not silent.
    assert not any("deduplicated" in w for w in first.warnings)
    assert any(
        w == "detail_link_deduplicated:https://example.com/events/food-fest"
        for w in second.warnings
    )
    assert not any("deduplicated" in w for w in third.warnings)


@pytest.mark.asyncio
async def test_detail_page_fetch_cap_skips_and_warns_deterministically():
    """max_detail_fetches bounds *distinct* detail URLs fetched per run.
    Once the cap is hit, later cards are left unenriched with a warning
    rather than silently dropped or fetched anyway."""
    fetched_urls: list[str] = []

    class RecordingFetch:
        async def fetch(self, request, config):
            fetched_urls.append(request.url)
            return make_response_from_fixture("static_html_detail_page.html", final_url=request.url)

    detail_config = SiteConfiguration(
        pattern_name="generic_html_cards",
        listing_url="https://example.com/events",
        event_container_selector=".event-card",
        max_detail_fetches=1,
        field_selectors={
            **FIELD_SELECTORS,
            "detail_description": {"kind": "css", "selector": ".full-description"},
        },
    )
    response = make_response_from_fixture(
        "static_html_cards_shared_detail.html", final_url="https://example.com/events"
    )
    candidates = StaticHtmlCardsPattern().extract(response, detail_config)
    enriched = await enrich_with_detail_pages(candidates, RecordingFetch(), detail_config)

    # Only the first distinct URL (food-fest) is ever fetched — deterministic,
    # first-seen order — even though a second, different URL (art-walk) exists.
    assert fetched_urls == ["https://example.com/events/food-fest"]

    first, second, third = enriched
    assert first.raw["description"] is not None
    # Second card reuses the cached (already-fetched) food-fest response.
    assert second.raw["description"] is not None
    # Third card's URL (art-walk) was never fetched: over the cap, skipped.
    assert third.raw.get("description") is None
    assert any(
        w == "detail_page_fetch_skipped_max_reached:https://example.com/events/art-walk"
        for w in third.warnings
    )


def test_css_selector_validation_accepts_valid_and_rejects_invalid():
    assert validate_css_selector(".event-card") == ".event-card"
    with pytest.raises(InvalidSelectorError):
        validate_css_selector(":::not-a-selector:::")


def test_field_selector_config_rejects_overlong_selector():
    with pytest.raises(ValidationError):
        FieldSelectorConfig(kind="css", selector="a" * 501)
