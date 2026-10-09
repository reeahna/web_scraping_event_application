"""Website URL matching: deterministic, lifecycle-aware ranking.

The failure this guards against: an archived row sharing a URL with the live
row that replaced it could be returned first purely because it sorted earlier
by id, blocking onboarding or re-detection of a perfectly good source.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.core.onboarding import ARCHIVED
from app.core.onboarding_jobs import DUPLICATE
from app.models.website import Website
from app.repositories.onboarding import (
    MATCH_BASE_URL,
    MATCH_LISTING_URL,
    MATCH_PAGE_UNDER_ORIGIN,
    REASON_ARCHIVED_EXISTING,
    REASON_EXISTING,
    find_website_match,
    find_website_matches,
)
from app.services.bulk_onboarding import create_batch_from_submission, process_batch
from app.services.onboarding_submission import SubmissionLimits, parse_url_lines
from tests.extraction_helpers import load_fixture, patched_http_fetch

LISTING_URL = "https://venue.example.org/events"
ORIGIN = "https://venue.example.org"
LIMITS = SubmissionLimits(
    max_urls=50, max_csv_rows=50, max_csv_bytes=100_000, max_url_length=2000
)


@pytest.fixture
def city(make_city):
    return make_city(name="Matcher City", slug="matcher-city", timezone="UTC")


def _website(
    db_session,
    make_website,
    *,
    city,
    name,
    base_url=ORIGIN,
    listing_url=LISTING_URL,
    archived=False,
    active=False,
    approved=None,
):
    website = make_website(
        city, name=name, base_url=base_url, is_active=active, approved_pattern=approved
    )
    website.event_listing_url = listing_url
    if archived:
        website.onboarding_status = ARCHIVED
        from datetime import UTC, datetime

        website.archived_at = datetime.now(UTC)
    elif active:
        website.onboarding_status = "active"
    db_session.commit()
    return website


# --- lifecycle ranking -------------------------------------------------------


def test_an_active_website_is_preferred_over_an_archived_one(db_session, city, make_website):
    # Archived row created FIRST, so a lowest-id-wins matcher would pick it.
    archived = _website(db_session, make_website, city=city, name="Old", archived=True)
    live = _website(db_session, make_website, city=city, name="Live", active=True)

    match = find_website_match(db_session, LISTING_URL)
    assert match.website.id == live.id
    assert archived.id < live.id
    assert match.is_archived is False
    assert match.reason == REASON_EXISTING


def test_a_non_archived_inactive_website_is_preferred_over_an_archived_one(
    db_session, city, make_website
):
    archived = _website(db_session, make_website, city=city, name="Old", archived=True)
    draft = _website(db_session, make_website, city=city, name="Draft")

    match = find_website_match(db_session, LISTING_URL)
    assert match.website.id == draft.id
    assert archived.id < draft.id
    assert match.is_archived is False


def test_an_active_website_outranks_a_non_archived_inactive_one(db_session, city, make_website):
    draft = _website(db_session, make_website, city=city, name="Draft")
    live = _website(db_session, make_website, city=city, name="Live", active=True)

    match = find_website_match(db_session, LISTING_URL)
    assert match.website.id == live.id
    assert draft.id < live.id


def test_lifecycle_outranks_match_specificity(db_session, city, make_website):
    """A weaker match on a live row still beats a stronger match on an
    archived one — a dead row must never shadow a living source."""
    archived = _website(db_session, make_website, city=city, name="Old Exact", archived=True)
    live = _website(
        db_session, make_website, city=city, name="Live Origin", listing_url=None, active=True
    )

    match = find_website_match(db_session, LISTING_URL)
    assert match.website.id == live.id
    assert match.match_type == MATCH_PAGE_UNDER_ORIGIN
    # The archived exact match is still reported, just ranked below.
    assert [m.website.id for m in find_website_matches(db_session, LISTING_URL)] == [
        live.id,
        archived.id,
    ]


# --- specificity ranking within one lifecycle group --------------------------


def test_exact_listing_url_is_preferred_over_a_page_under_origin_match(
    db_session, city, make_website
):
    origin_only = _website(
        db_session, make_website, city=city, name="Whole Site", listing_url=None
    )
    exact = _website(db_session, make_website, city=city, name="Exact Listing")

    match = find_website_match(db_session, LISTING_URL)
    assert match.website.id == exact.id
    assert match.match_type == MATCH_LISTING_URL
    assert origin_only.id < exact.id


def test_exact_base_url_is_preferred_over_a_page_under_origin_match(
    db_session, city, make_website
):
    origin_only = _website(
        db_session, make_website, city=city, name="Whole Site", listing_url=None
    )
    base_exact = _website(
        db_session,
        make_website,
        city=city,
        name="Base Exact",
        base_url="https://venue.example.org/events",
        listing_url=None,
    )

    match = find_website_match(db_session, LISTING_URL)
    assert match.website.id == base_exact.id
    assert match.match_type == MATCH_BASE_URL
    assert origin_only.id < base_exact.id


def test_lowest_id_is_the_final_tiebreaker(db_session, city, make_website):
    first = _website(db_session, make_website, city=city, name="First")
    _website(db_session, make_website, city=city, name="Second")

    match = find_website_match(db_session, LISTING_URL)
    assert match.website.id == first.id
    assert match.priority == (1, 0, first.id)


# --- archived-only matches ---------------------------------------------------


def test_an_archived_only_match_is_returned_and_labelled(db_session, city, make_website):
    archived = _website(db_session, make_website, city=city, name="Archived Only", archived=True)

    match = find_website_match(db_session, LISTING_URL)
    assert match is not None, "an archived match must never be silently dropped"
    assert match.website.id == archived.id
    assert match.is_archived is True
    assert match.reason == REASON_ARCHIVED_EXISTING
    assert "archived" in match.description


# --- both callers share the matcher ------------------------------------------


def _handler(routes: dict[str, str]):
    def handler(request: httpx.Request) -> httpx.Response:
        body = routes.get(str(request.url))
        if body is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    return handler


def _run_batch(db_session, city, url=LISTING_URL, *, redetect=False):
    parsed = parse_url_lines(url, LIMITS)
    batch = create_batch_from_submission(
        db_session,
        parsed,
        submitted_by_user_id=None,
        default_city_id=city.id,
        default_timezone=None,
        redetect_existing=redetect,
        source_kind="single",
        correlation_id="matcher-test",
    )
    fixture = load_fixture("inference_cards_slash_year_date.html")
    with patched_http_fetch(_handler({url: fixture})):
        asyncio.run(process_batch(db_session, batch, limit=5))
    db_session.refresh(batch)
    return batch


def test_bulk_onboarding_links_the_live_website_not_the_archived_one(
    db_session, city, make_website
):
    _website(db_session, make_website, city=city, name="Old", archived=True)
    live = _website(db_session, make_website, city=city, name="Live", active=True)
    before = db_session.query(Website).count()

    job = _run_batch(db_session, city).jobs[0]
    assert job.status == DUPLICATE
    assert job.duplicate_of_website_id == live.id
    assert db_session.query(Website).count() == before


def test_an_archived_only_match_does_not_create_a_replacement_website(
    db_session, city, make_website
):
    archived = _website(db_session, make_website, city=city, name="Archived Only", archived=True)
    before = db_session.query(Website).count()

    job = _run_batch(db_session, city).jobs[0]
    assert job.status == DUPLICATE
    assert job.duplicate_of_website_id == archived.id
    assert db_session.query(Website).count() == before, "must not silently replace an archived row"
    assert "archived" in job.failure_reason.lower()


def test_an_archived_match_is_never_re_detected_even_with_redetect_enabled(
    db_session, city, make_website
):
    archived = _website(db_session, make_website, city=city, name="Archived Only", archived=True)
    before = db_session.query(Website).count()

    job = _run_batch(db_session, city, redetect=True).jobs[0]
    db_session.refresh(archived)

    assert job.status == DUPLICATE
    assert archived.configuration is None, "an archived source must not receive a draft"
    assert archived.onboarding_status == ARCHIVED
    assert db_session.query(Website).count() == before


def test_an_approved_active_website_is_untouched_by_a_match(db_session, city, make_website):
    approved = {"pattern_name": "generic_html_cards", "listing_url": LISTING_URL}
    _website(db_session, make_website, city=city, name="Old", archived=True)
    live = _website(
        db_session, make_website, city=city, name="Live", active=True, approved=approved
    )

    _run_batch(db_session, city)
    db_session.refresh(live)

    assert live.approved_pattern == approved
    assert live.onboarding_status == "active"
    assert live.is_active is True
    assert live.configuration is None


def test_the_manual_form_and_bulk_onboarding_agree_on_the_same_match(
    client, db_session, city, make_website, make_super_admin, login
):
    """Both entry points must resolve one URL to the same website — the
    ranking lives in one place and neither caller re-implements it."""
    _website(db_session, make_website, city=city, name="Old", archived=True)
    live = _website(db_session, make_website, city=city, name="Live", active=True)
    make_super_admin(email="matcher-root@example.com", password="root-pass-1234")
    login("matcher-root@example.com", "root-pass-1234")
    before = db_session.query(Website).count()

    resp = client.post(
        "/admin/websites",
        data={
            "name": "Manual Duplicate",
            "source_display_name": "",
            "city_id": str(city.id),
            "base_url": ORIGIN,
            "event_listing_url": LISTING_URL,
            "timezone_override": "",
            "schedule_config": "",
            "csrf_token": client.cookies.get("csrf_token"),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 409
    assert f"website #{live.id}" in resp.text
    assert db_session.query(Website).count() == before

    job = _run_batch(db_session, city).jobs[0]
    assert job.duplicate_of_website_id == live.id


def test_the_manual_form_blocks_an_archived_match_and_explains_it(
    client, db_session, city, make_website, make_super_admin, login
):
    archived = _website(db_session, make_website, city=city, name="Archived Only", archived=True)
    make_super_admin(email="matcher-root2@example.com", password="root-pass-1234")
    login("matcher-root2@example.com", "root-pass-1234")
    before = db_session.query(Website).count()

    resp = client.post(
        "/admin/websites",
        data={
            "name": "Replacement Attempt",
            "source_display_name": "",
            "city_id": str(city.id),
            "base_url": ORIGIN,
            "event_listing_url": LISTING_URL,
            "timezone_override": "",
            "schedule_config": "",
            "csrf_token": client.cookies.get("csrf_token"),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 409
    assert "archived" in resp.text.lower()
    assert f"/admin/websites/{archived.id}" in resp.text
    assert db_session.query(Website).count() == before
    db_session.refresh(archived)
    assert archived.onboarding_status == ARCHIVED, "matching must never unarchive anything"


# --- provider independence ---------------------------------------------------


def test_the_matcher_contains_no_hostname_specific_logic():
    """Scans executable code only — comments and docstrings may legitimately
    use example.org to explain a rule; what must not exist is a *literal* the
    matcher can branch on."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path("app/repositories/onboarding.py").read_text(encoding="utf-8"))
    docstring_nodes = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    executable = [
        node.value.lower()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstring_nodes
    ]

    for value in executable:
        assert "://" not in value, value
        for token in ("iuauditorium", "buskirk", "chumley", ".org", ".com", ".edu"):
            assert token not in value, value
