"""Site-metadata inference: what a source is called, without asking anyone."""

from __future__ import annotations

from app.extraction.inference.site_metadata import (
    hostname_label,
    infer_site_metadata,
    normalize_origin,
)

URL = "https://www.riverside-arts.example.org/events/"


def _infer(document: str | None, **kwargs):
    return infer_site_metadata(
        document=document, final_url=URL, submitted_url=URL, **kwargs
    )


def test_open_graph_site_name_wins():
    document = """
    <html><head>
      <meta property="og:site_name" content="Riverside Arts Center" />
      <title>Events | Something Else</title>
    </head><body></body></html>
    """
    metadata = _infer(document)
    assert metadata.name == "Riverside Arts Center"
    assert metadata.inferred_fields["name"] == "og:site_name"


def test_structured_organization_name_is_used_when_open_graph_is_absent():
    document = """
    <html><head>
      <script type="application/ld+json">
        {"@context":"https://schema.org","@type":"Organization","name":"Riverside Arts Center"}
      </script>
      <title>Events</title>
    </head><body></body></html>
    """
    metadata = _infer(document)
    assert metadata.name == "Riverside Arts Center"
    assert "Organization" in metadata.inferred_fields["name"]


def test_html_title_fallback_strips_the_page_section():
    document = (
        "<html><head><title>Events | Riverside Arts Center</title></head><body></body></html>"
    )
    metadata = _infer(document)
    assert metadata.name == "Riverside Arts Center"
    assert "<title>" in metadata.inferred_fields["name"]


def test_html_title_without_a_separator_is_used_whole():
    document = "<html><head><title>Riverside Arts Center</title></head><body></body></html>"
    assert _infer(document).name == "Riverside Arts Center"


def test_hostname_fallback_when_the_page_says_nothing():
    metadata = _infer("<html><head></head><body>No title here.</body></html>")
    assert metadata.name == "Riverside Arts"
    assert metadata.inferred_fields["name"] == "hostname fallback"


def test_hostname_fallback_when_the_page_could_not_be_fetched():
    metadata = _infer(None)
    assert metadata.name == "Riverside Arts"


def test_a_supplied_name_is_never_overridden():
    document = '<html><head><meta property="og:site_name" content="Ignored" /></head></html>'
    metadata = _infer(document, supplied_name="Operator Chosen Name")
    assert metadata.name == "Operator Chosen Name"
    assert "name" not in metadata.inferred_fields


def test_source_display_name_defaults_to_the_site_name():
    document = '<html><head><meta property="og:site_name" content="Riverside Arts" /></head></html>'
    metadata = _infer(document)
    assert metadata.source_display_name == "Riverside Arts"


def test_base_url_is_the_normalized_origin_and_listing_url_is_what_was_submitted():
    metadata = _infer(None)
    assert metadata.base_url == "https://www.riverside-arts.example.org"
    assert metadata.event_listing_url == URL


def test_origin_and_hostname_helpers():
    assert normalize_origin("HTTPS://Example.ORG/a/b?c=1#f") == "https://example.org"
    assert normalize_origin("https://example.org:8443/a") == "https://example.org:8443"
    assert hostname_label("https://www.grand-street.example.org/x") == "Grand Street"
    assert hostname_label("https://example.org/x") == "Example"
