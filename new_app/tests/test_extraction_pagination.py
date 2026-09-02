from app.extraction.pagination import (
    NextLinkPagination,
    NonePagination,
    QueryParamPagination,
    WordPressPagination,
    build_pagination_strategy,
)
from app.extraction.types import FetchRequest
from app.schemas.extraction import SiteConfiguration
from tests.extraction_helpers import make_response

BASE_CONFIG = SiteConfiguration(
    pattern_name="generic_html_cards",
    listing_url="https://example.com/events",
    event_container_selector=".event-card",
    pagination={"strategy": "query_param", "page_param": "page", "max_pages": 3},
)


def test_none_pagination_never_continues():
    response = make_response("<html></html>", final_url="https://example.com/events")
    result = NonePagination().next_request(
        response, 0, BASE_CONFIG, visited_urls=frozenset(), seen_body_hashes=frozenset()
    )
    assert result is None


def test_query_param_pagination_increments_page():
    response = make_response("<html></html>", final_url="https://example.com/events")
    next_request = QueryParamPagination().next_request(
        response, 0, BASE_CONFIG, visited_urls=frozenset(), seen_body_hashes=frozenset()
    )
    assert isinstance(next_request, FetchRequest)
    assert next_request.url == "https://example.com/events?page=2"


def test_query_param_pagination_stops_at_max_pages():
    response = make_response("<html></html>", final_url="https://example.com/events")
    config = SiteConfiguration(
        pattern_name="generic_html_cards",
        listing_url="https://example.com/events",
        event_container_selector=".event-card",
        pagination={"strategy": "query_param", "page_param": "page", "max_pages": 1},
    )
    result = QueryParamPagination().next_request(
        response, 0, config, visited_urls=frozenset(), seen_body_hashes=frozenset()
    )
    assert result is None


def test_pagination_stops_on_repeated_body_hash():
    response = make_response(
        "<html>same content</html>", final_url="https://example.com/events?page=2"
    )
    seen = frozenset({response.body_hash})
    result = QueryParamPagination().next_request(
        response, 0, BASE_CONFIG, visited_urls=frozenset(), seen_body_hashes=seen
    )
    assert result is None


def test_pagination_stops_on_already_visited_url():
    response = make_response("<html></html>", final_url="https://example.com/events")
    result = QueryParamPagination().next_request(
        response,
        0,
        BASE_CONFIG,
        visited_urls=frozenset({"https://example.com/events?page=2"}),
        seen_body_hashes=frozenset(),
    )
    assert result is None


def test_wordpress_pagination_respects_total_pages_header():
    response = make_response(
        "[]",
        final_url="https://example.com/wp-json/wp/v2/events?page=1",
        headers={"x-wp-totalpages": "1"},
        content_type="application/json",
    )
    config = SiteConfiguration(
        pattern_name="wordpress_rest",
        api_endpoint="https://example.com/wp-json/wp/v2/events",
        pagination={"strategy": "wordpress", "max_pages": 10},
    )
    result = WordPressPagination().next_request(
        response, 0, config, visited_urls=frozenset(), seen_body_hashes=frozenset()
    )
    assert result is None  # already on the only page


def test_wordpress_pagination_continues_when_more_pages_remain():
    response = make_response(
        "[]",
        final_url="https://example.com/wp-json/wp/v2/events?page=1",
        headers={"x-wp-totalpages": "3"},
        content_type="application/json",
    )
    config = SiteConfiguration(
        pattern_name="wordpress_rest",
        api_endpoint="https://example.com/wp-json/wp/v2/events",
        pagination={"strategy": "wordpress", "max_pages": 10},
    )
    result = WordPressPagination().next_request(
        response, 0, config, visited_urls=frozenset(), seen_body_hashes=frozenset()
    )
    assert result is not None
    assert "page=2" in result.url


def test_next_link_pagination_requires_explicit_selector():
    response = make_response(
        '<html><a rel="next" href="/page/2">Next</a></html>', final_url="https://example.com/events"
    )
    config = SiteConfiguration(
        pattern_name="generic_html_cards",
        listing_url="https://example.com/events",
        event_container_selector=".event-card",
        pagination={"strategy": "next_link", "max_pages": 5},
    )
    # No next_page_selector configured -> never follows, never guesses.
    result = NextLinkPagination(None).next_request(
        response, 0, config, visited_urls=frozenset(), seen_body_hashes=frozenset()
    )
    assert result is None

    result_configured = NextLinkPagination("a[rel='next']").next_request(
        response, 0, config, visited_urls=frozenset(), seen_body_hashes=frozenset()
    )
    assert result_configured is not None
    assert result_configured.url == "https://example.com/page/2"


def test_build_pagination_strategy_dispatches_by_name():
    assert isinstance(build_pagination_strategy(BASE_CONFIG), QueryParamPagination)


# --- path_page pagination (numbered pages in the URL path) --------------------

from app.extraction.inference.html_fields import detect_path_pagination  # noqa: E402
from app.extraction.pagination import PathPagePagination  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

PATH_CONFIG = SiteConfiguration(
    pattern_name="json_ld_event",
    listing_url="https://example.com/calendar/upcoming",
    pagination={
        "strategy": "path_page",
        "page_path_template": "https://example.com/calendar/upcoming/{page}",
        "max_pages": 3,
    },
)


def test_path_page_pagination_appends_the_page_number():
    response = make_response("<html></html>", final_url="https://example.com/calendar/upcoming")
    nxt = PathPagePagination(
        "https://example.com/calendar/upcoming/{page}"
    ).next_request(response, 0, PATH_CONFIG, visited_urls=frozenset(), seen_body_hashes=frozenset())
    assert isinstance(nxt, FetchRequest)
    assert nxt.url == "https://example.com/calendar/upcoming/2"


def test_path_page_pagination_stops_at_max_pages():
    response = make_response("<html></html>", final_url="https://example.com/calendar/upcoming/3")
    result = PathPagePagination(
        "https://example.com/calendar/upcoming/{page}"
    ).next_request(response, 2, PATH_CONFIG, visited_urls=frozenset(), seen_body_hashes=frozenset())
    assert result is None


def test_path_page_pagination_stops_on_duplicate_body():
    # A throttling site that re-serves an earlier page must halt the walk.
    response = make_response("<html>dup</html>", final_url="https://example.com/calendar/upcoming/2")
    result = PathPagePagination(
        "https://example.com/calendar/upcoming/{page}"
    ).next_request(
        response, 1, PATH_CONFIG,
        visited_urls=frozenset(), seen_body_hashes=frozenset({response.body_hash}),
    )
    assert result is None


def test_build_pagination_strategy_selects_path_page():
    assert isinstance(build_pagination_strategy(PATH_CONFIG), PathPagePagination)


def test_detect_path_pagination_from_numbered_path_links():
    html = """
    <div class="pager">
      <div class="active">1</div>
      <a href="/calendar/upcoming/2">2</a>
      <a href="/calendar/upcoming/3">3</a>
    </div>
    """
    soup = BeautifulSoup(html, "html.parser")
    template = detect_path_pagination(soup, "https://example.com/calendar/upcoming")
    assert template == "https://example.com/calendar/upcoming/{page}"


def test_detect_path_pagination_handles_page_segment():
    html = '<a href="/events/page/2/">Next</a>'
    soup = BeautifulSoup(html, "html.parser")
    template = detect_path_pagination(soup, "https://example.com/events")
    # The trailing slash from the source link is preserved so later pages match.
    assert template == "https://example.com/events/page/{page}/"


def test_detect_path_pagination_ignores_cross_origin_and_page_one():
    html = '<a href="https://other.com/events/2">x</a><a href="/events">home</a>'
    soup = BeautifulSoup(html, "html.parser")
    assert detect_path_pagination(soup, "https://example.com/events") is None
