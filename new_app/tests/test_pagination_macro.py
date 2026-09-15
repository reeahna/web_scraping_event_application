"""The shared windowed pagination control (app/templates/_pagination.html).

Exercised through the admin events list, which is one of its three callers.
The window keeps the control a fixed maximum width — first page, last page,
and two either side of the current one — so it does not reflow as you click
through a long list.
"""

import re

import pytest

from app.core.permissions import ADMINISTRATOR

PER_PAGE = 20
TOTAL = 149  # 8 pages


@pytest.fixture
def paged_events(client, make_user, make_city, make_event, login):
    make_user(email="pager@example.com", password="pager-pass-123", role_name=ADMINISTRATOR)
    login("pager@example.com", "pager-pass-123")
    city = make_city()
    for index in range(TOTAL):
        make_event(
            city,
            title=f"Pager Event {index:03d}",
            canonical_url=f"https://example.com/pager/{index}",
        )
    return client


def _control_text(html: str) -> str:
    """The pagination control rendered as plain text, e.g. "1 2 3 4 5 … 8"."""
    nav = re.search(r'<nav class="pagination".*?</nav>', html, re.S).group(0)
    controls = re.search(r'<div class="pagination-controls">.*?</div>', nav, re.S).group(0)
    stripped = re.sub(r"<[^>]+>", " ", controls)
    return " ".join(stripped.split()).replace("&larr;", "<").replace("&rarr;", ">")


@pytest.mark.parametrize(
    "page,expected",
    [
        (1, "< 1 2 3 … 8 >"),
        (2, "< 1 2 3 4 … 8 >"),
        (4, "< 1 2 3 4 5 6 … 8 >"),
        (6, "< 1 … 4 5 6 7 8 >"),
        (8, "< 1 … 6 7 8 >"),
    ],
)
def test_window_shows_first_last_and_two_either_side(paged_events, page, expected):
    response = paged_events.get(f"/admin/events?page={page}")
    assert response.status_code == 200
    assert _control_text(response.text) == expected


@pytest.mark.parametrize("page,first,last", [(1, 1, 20), (4, 61, 80), (8, 141, 149)])
def test_range_readout_counts_items_not_pages(paged_events, page, first, last):
    response = paged_events.get(f"/admin/events?page={page}")
    assert f"Showing {first}\u2013{last} of {TOTAL}" in response.text


def test_arrows_are_disabled_not_removed_at_the_ends(paged_events):
    """If the arrows vanished at the ends, the numbers would shift sideways
    exactly when the reader lands on the first or last page."""
    for page in (1, 8):
        html = paged_events.get(f"/admin/events?page={page}").text
        nav = re.search(r'<nav class="pagination".*?</nav>', html, re.S).group(0)
        assert nav.count("pagination-step") == 2
        assert nav.count('aria-disabled="true"') == 1

    middle = paged_events.get("/admin/events?page=4").text
    assert 'aria-disabled="true"' not in middle


def test_current_page_is_not_a_link(paged_events):
    html = paged_events.get("/admin/events?page=4").text
    assert '<span class="pagination-page is-current" aria-current="page">4</span>' in html
    assert 'aria-label="Page 4"' not in html


def test_single_page_result_has_no_gap_and_both_arrows_disabled(
    client, make_user, make_city, make_event, login
):
    make_user(email="onepage@example.com", password="pager-pass-123", role_name=ADMINISTRATOR)
    login("onepage@example.com", "pager-pass-123")
    make_event(make_city(), title="Only", canonical_url="https://example.com/only")

    html = client.get("/admin/events").text
    assert _control_text(html) == "< 1 >"
    assert "Showing 1\u20131 of 1" in html
