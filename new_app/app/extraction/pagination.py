"""Pagination foundation.

Supports exactly four strategies this phase: none, numbered query-parameter
pagination, WordPress REST page pagination, and an explicit next-link
selector for static HTML. No load-more, no infinite scroll, no browser
network interception.

Each strategy is a pure function of the current response + page index +
config + the caller's own visited-state (never internal mutable state) —
the calling loop (app.services.extraction_runs) owns `visited_urls` and
`seen_body_hashes` and is what actually enforces determinism: page order is
whatever the strategy computes from `page_index`, never an unordered
dict/set iteration.
"""

from __future__ import annotations

import json
import re
from typing import Protocol
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from app.extraction.network_safety import BlockedHostError, validate_request_url
from app.extraction.types import FetchRequest, FetchResponse
from app.schemas.extraction import SiteConfiguration


class PaginationStrategy(Protocol):
    def next_request(
        self,
        response: FetchResponse,
        page_index: int,
        config: SiteConfiguration,
        *,
        visited_urls: frozenset[str],
        seen_body_hashes: frozenset[str],
    ) -> FetchRequest | None: ...


def _safe_or_none(url: str) -> str | None:
    try:
        return validate_request_url(url)
    except BlockedHostError:
        return None


def _stop_conditions_met(
    response: FetchResponse,
    page_index: int,
    config: SiteConfiguration,
    visited_urls: frozenset[str],
    seen_body_hashes: frozenset[str],
) -> bool:
    if page_index + 1 >= config.pagination.max_pages:
        return True
    if response.body_hash in seen_body_hashes:
        return True
    return response.final_url in visited_urls


class NonePagination:
    def next_request(self, response, page_index, config, *, visited_urls, seen_body_hashes):
        return None


class QueryParamPagination:
    """Numbered query-parameter pagination — e.g. ?page=2, ?offset=20."""

    def next_request(
        self,
        response: FetchResponse,
        page_index: int,
        config: SiteConfiguration,
        *,
        visited_urls,
        seen_body_hashes,
    ) -> FetchRequest | None:
        if _stop_conditions_met(response, page_index, config, visited_urls, seen_body_hashes):
            return None
        page_param = config.pagination.page_param or "page"
        next_page = page_index + 2  # page_index is 0-based; humans count from 1
        next_url = _next_url_with_param(response.final_url, page_param, str(next_page))
        safe_url = _safe_or_none(next_url)
        if safe_url is None or safe_url in visited_urls:
            return None
        return FetchRequest(url=safe_url)


class WordPressPagination:
    """WordPress REST pagination via X-WP-TotalPages."""

    def next_request(
        self,
        response: FetchResponse,
        page_index: int,
        config: SiteConfiguration,
        *,
        visited_urls,
        seen_body_hashes,
    ) -> FetchRequest | None:
        if _stop_conditions_met(response, page_index, config, visited_urls, seen_body_hashes):
            return None
        total_pages_header = response.headers.get("x-wp-totalpages")
        try:
            total_pages = int(total_pages_header) if total_pages_header else None
        except ValueError:
            total_pages = None
        next_page = page_index + 2
        if total_pages is not None and next_page > total_pages:
            return None
        page_param = config.pagination.page_param or "page"
        next_url = _next_url_with_param(response.final_url, page_param, str(next_page))
        safe_url = _safe_or_none(next_url)
        if safe_url is None or safe_url in visited_urls:
            return None
        return FetchRequest(url=safe_url)


class NextLinkPagination:
    """Static HTML: follows an explicitly configured next-page selector.
    Never inferred/guessed — only used when the site configuration sets a
    next-page selector."""

    def __init__(self, next_page_selector: str | None) -> None:
        self._selector = next_page_selector

    def next_request(
        self,
        response: FetchResponse,
        page_index: int,
        config: SiteConfiguration,
        *,
        visited_urls,
        seen_body_hashes,
    ) -> FetchRequest | None:
        if self._selector is None:
            return None
        if _stop_conditions_met(response, page_index, config, visited_urls, seen_body_hashes):
            return None
        soup = BeautifulSoup(response.text, "html.parser")
        link = soup.select_one(self._selector)
        if link is None or not link.get("href"):
            return None
        next_url = urljoin(response.final_url, link["href"])
        safe_url = _safe_or_none(next_url)
        if safe_url is None or safe_url in visited_urls:
            return None
        return FetchRequest(url=safe_url)


class TribeRestPagination:
    """The Events Calendar REST API pagination — driven entirely by the JSON
    response body's own `next_rest_url` (this endpoint doesn't set the WP
    core X-WP-TotalPages header), so it is not just QueryParamPagination
    with a different param name."""

    def next_request(
        self,
        response: FetchResponse,
        page_index: int,
        config: SiteConfiguration,
        *,
        visited_urls,
        seen_body_hashes,
    ) -> FetchRequest | None:
        if _stop_conditions_met(response, page_index, config, visited_urls, seen_body_hashes):
            return None
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        next_url = payload.get("next_rest_url")
        if not next_url or not isinstance(next_url, str):
            return None
        next_url = urljoin(response.final_url, next_url)
        safe_url = _safe_or_none(next_url)
        if safe_url is None or safe_url in visited_urls:
            return None
        return FetchRequest(url=safe_url)


class LiveWhaleOffsetPagination:
    """LiveWhale-style offset pagination: no page number, no next-page URL in
    the body — just an ever-increasing `offset` query parameter. The next
    offset is computed from how many events this page's own response body
    actually contained (never a guessed/configured page size), so it works
    whether or not the admin also configured a limit parameter. `page_param`
    is reused here as the offset parameter's name, the same way it's already
    reused for a page number elsewhere in this module — no new schema field.

    Known limitation, shared with QueryParamPagination/WordPressPagination
    (not new here): `config.fetch.query_params` (e.g. group/tag filters) are
    only applied to the *first* request, via httpx's own params merging —
    FetchResponse.final_url never reflects them, so a next URL rebuilt from
    final_url (as every strategy in this module except TribeRestPagination
    does) can't carry them forward either. TribeRestPagination avoids this
    because the remote server's own `next_rest_url` already echoes the full
    query string back. A shared-core fix (e.g. threading the original
    request's static params through every subsequent page) would need to be
    made once, for all affected strategies — out of scope here."""

    def next_request(
        self,
        response: FetchResponse,
        page_index: int,
        config: SiteConfiguration,
        *,
        visited_urls,
        seen_body_hashes,
    ) -> FetchRequest | None:
        if _stop_conditions_met(response, page_index, config, visited_urls, seen_body_hashes):
            return None
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError:
            return None
        if isinstance(payload, dict):
            events = payload.get("events")
        elif isinstance(payload, list):
            events = payload
        else:
            events = None
        if not isinstance(events, list) or not events:
            return None

        offset_param = config.pagination.page_param or "offset"
        current_offset_values = parse_qs(urlsplit(response.final_url).query).get(offset_param)
        try:
            current_offset = int(current_offset_values[0]) if current_offset_values else 0
        except (TypeError, ValueError):
            current_offset = 0

        next_offset = current_offset + len(events)
        next_url = _next_url_with_param(response.final_url, offset_param, str(next_offset))
        safe_url = _safe_or_none(next_url)
        if safe_url is None or safe_url in visited_urls:
            return None
        return FetchRequest(url=safe_url)


_QUERY_PARAM_RE_TEMPLATE = r"([?&]{param}=)[^&]*"


def _next_url_with_param(url: str, param: str, value: str) -> str:
    pattern = re.compile(_QUERY_PARAM_RE_TEMPLATE.format(param=re.escape(param)))
    if pattern.search(url):
        return pattern.sub(rf"\g<1>{value}", url, count=1)
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{param}={value}"


def build_pagination_strategy(config: SiteConfiguration) -> PaginationStrategy:
    strategy = config.pagination.strategy
    if strategy == "none":
        return NonePagination()
    if strategy == "query_param":
        return QueryParamPagination()
    if strategy == "wordpress":
        return WordPressPagination()
    if strategy == "next_link":
        return NextLinkPagination(config.pagination.next_page_selector)
    if strategy == "tribe_rest":
        return TribeRestPagination()
    if strategy == "livewhale_offset":
        return LiveWhaleOffsetPagination()
    raise ValueError(f"Unknown pagination strategy: {strategy}")
