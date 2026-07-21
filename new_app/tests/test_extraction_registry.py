import pytest
from pydantic import ValidationError

from app.extraction.registry import (
    DuplicatePatternError,
    PatternRegistration,
    PatternRegistry,
    UnsupportedPatternError,
    build_default_registry,
)
from app.schemas.extraction import SiteConfiguration


def _dummy_registration(name: str) -> PatternRegistration:
    return PatternRegistration(
        name=name,
        detector=object(),
        extractor=object(),
        config_schema=SiteConfiguration,
        priority=0,
        version="1",
        browser_required=False,
        supported_pagination=("none",),
    )


def test_registry_registers_and_retrieves_patterns():
    registry = PatternRegistry()
    registry.register(_dummy_registration("test_pattern"))
    assert registry.get("test_pattern").name == "test_pattern"
    assert "test_pattern" in registry.names()


def test_duplicate_pattern_name_rejected():
    registry = PatternRegistry()
    registry.register(_dummy_registration("test_pattern"))
    with pytest.raises(DuplicatePatternError):
        registry.register(_dummy_registration("test_pattern"))


def test_unknown_pattern_rejected():
    registry = PatternRegistry()
    with pytest.raises(UnsupportedPatternError):
        registry.get("does_not_exist")


def test_default_registry_has_exactly_five_patterns():
    registry = build_default_registry()
    assert set(registry.names()) == {
        "json_ld_event",
        "generic_html_cards",
        "wordpress_rest",
        "the_events_calendar",
        "livewhale_json",
    }


# --- SiteConfiguration validation ------------------------------------------


def test_configuration_requires_listing_url_or_api_endpoint():
    with pytest.raises(ValidationError):
        SiteConfiguration(pattern_name="json_ld_event")


def test_configuration_accepts_minimal_valid_shape():
    config = SiteConfiguration(
        pattern_name="json_ld_event", listing_url="https://example.com/events"
    )
    assert config.pattern_name == "json_ld_event"


def test_unknown_top_level_field_rejected():
    with pytest.raises(ValidationError):
        SiteConfiguration(
            pattern_name="json_ld_event",
            listing_url="https://example.com/events",
            totally_unknown_field="oops",
        )


@pytest.mark.parametrize(
    "header", ["Host", "cookie", "AUTHORIZATION", "Content-Length", "Proxy-Authorization"]
)
def test_forbidden_headers_rejected(header):
    with pytest.raises(ValidationError):
        SiteConfiguration(
            pattern_name="json_ld_event",
            listing_url="https://example.com/events",
            fetch={"headers": {header: "value"}},
        )


def test_allowed_header_accepted():
    config = SiteConfiguration(
        pattern_name="json_ld_event",
        listing_url="https://example.com/events",
        fetch={"headers": {"X-Custom-Header": "value"}},
    )
    assert config.fetch.headers["X-Custom-Header"] == "value"


@pytest.mark.parametrize("value", ["${SECRET_TOKEN}", "$HOME", "%APPDATA%"])
def test_env_var_reference_in_header_value_rejected(value):
    with pytest.raises(ValidationError):
        SiteConfiguration(
            pattern_name="json_ld_event",
            listing_url="https://example.com/events",
            fetch={"headers": {"X-Token": value}},
        )


def test_arbitrary_http_method_rejected():
    with pytest.raises(ValidationError):
        SiteConfiguration(
            pattern_name="json_ld_event",
            listing_url="https://example.com/events",
            fetch={"method": "DELETE"},
        )


def test_post_without_body_or_params_rejected():
    with pytest.raises(ValidationError):
        SiteConfiguration(
            pattern_name="wordpress_rest",
            api_endpoint="https://example.com/wp-json/wp/v2/events",
            fetch={"method": "POST"},
        )


def test_post_with_json_body_accepted():
    config = SiteConfiguration(
        pattern_name="wordpress_rest",
        api_endpoint="https://example.com/wp-json/wp/v2/events",
        fetch={"method": "POST", "json_body": {"filter": "upcoming"}},
    )
    assert config.fetch.json_body == {"filter": "upcoming"}


def test_local_network_address_in_listing_url_rejected():
    with pytest.raises(ValidationError):
        SiteConfiguration(pattern_name="json_ld_event", listing_url="http://127.0.0.1/events")


def test_credentials_in_url_rejected():
    with pytest.raises(ValidationError):
        SiteConfiguration(
            pattern_name="json_ld_event", listing_url="https://user:pass@example.com/events"
        )


def test_transformation_rule_requires_field():
    with pytest.raises(ValidationError):
        SiteConfiguration(
            pattern_name="json_ld_event",
            listing_url="https://example.com/events",
            transformations=[{"kind": "trim"}],
        )
