"""Onboarding a whole college town with Claude: the proposal checks, the
conversation with the API (faked here), creating the town and its batch, and
the admin pages. No test calls the real API."""

from __future__ import annotations

import csv
import io
from types import SimpleNamespace

import pytest

from app.config import get_settings
from app.core.onboarding_jobs import SOURCE_CLAUDE
from app.models.city import City
from app.models.onboarding_batch import OnboardingBatch
from app.models.onboarding_job import OnboardingJob
from app.models.town_onboarding import (
    TOWN_DONE,
    TOWN_FAILED,
    TOWN_QUEUED,
    TownOnboardingRequest,
)
from app.services.town_onboarding import (
    TownOnboardingError,
    TownOnboardingNotConfigured,
    TownProposal,
    check_proposal,
    discover_town,
    process_next_request,
    proposal_csv,
    queue_town,
)

CAMPUS_PAGES = [
    ("https://events.umich.edu/list", "Happening @ Michigan"),
    ("https://maizepages.umich.edu/events", "Maize Pages Events"),
    ("https://ums.org/season/performances-all/", "UMS Performances"),
    ("https://mgoblue.com/calendar", "Michigan Athletics"),
]
TOWN_PAGES = [
    ("https://annarborobserver.com/calendar/list/", "Ann Arbor Observer"),
    ("https://theark.org/events/list/", "The Ark"),
    ("https://mainstreetannarbor.org/main-st-area-events", "Main Street Ann Arbor"),
    ("https://www.annarbor.org/events/", "Destination Ann Arbor"),
]


def _raw(campus=CAMPUS_PAGES, town=TOWN_PAGES, **overrides) -> dict:
    raw = {
        "city_name": "Ann Arbor",
        "state_or_region": "Michigan",
        "state_code": "MI",
        "country": "United States",
        "timezone": "America/Detroit",
        "latitude": 42.2808,
        "longitude": -83.743,
        "universities": ["University of Michigan"],
        "sources": [
            {"url": url, "display_name": name, "scope": "campus", "reason": "events"}
            for url, name in campus
        ]
        + [
            {"url": url, "display_name": name, "scope": "town", "reason": "events"}
            for url, name in town
        ],
    }
    raw.update(overrides)
    return raw


def _check(raw):
    return check_proposal(raw, min_sources=8, min_per_scope=3)


# --- the proposal checks -----------------------------------------------------


def test_a_full_proposal_is_accepted():
    proposal, problems = _check(_raw())
    assert problems == []
    assert proposal.slug == "ann-arbor-mi"
    assert proposal.count("campus") == 4 and proposal.count("town") == 4


def test_too_few_sources_is_refused():
    proposal, problems = _check(_raw(town=TOWN_PAGES[:3]))
    assert proposal is None
    assert any("at least 8" in p for p in problems)


def test_one_side_short_is_refused_even_with_enough_in_total():
    many_campus = CAMPUS_PAGES + [
        ("https://arts.umich.edu/events", "Arts at Michigan"),
        ("https://lsa.umich.edu/museum/events", "Natural History Museum"),
        ("https://umma.umich.edu/events", "UMMA"),
        ("https://rec.umich.edu/events", "Rec Sports"),
    ]
    proposal, problems = _check(_raw(campus=many_campus, town=TOWN_PAGES[:2]))
    assert proposal is None
    assert any("town source" in p for p in problems)


def test_social_media_duplicates_and_private_urls_are_dropped():
    raw = _raw()
    raw["sources"] += [
        {"url": "https://www.facebook.com/a2events", "display_name": "FB", "scope": "town",
         "reason": ""},
        {"url": "https://theark.org/events/list", "display_name": "The Ark", "scope": "town",
         "reason": ""},
        {"url": "http://127.0.0.1/events", "display_name": "Local", "scope": "town",
         "reason": ""},
    ]
    proposal, problems = _check(raw)
    assert proposal is not None
    assert len(proposal.sources) == 8
    assert len(proposal.rejected) == 3
    assert all(p.startswith("Dropped ") for p in problems)


def test_a_bad_timezone_is_refused():
    proposal, problems = _check(_raw(timezone="EST5EDT-ish"))
    assert proposal is None
    assert any("timezone" in p for p in problems)


def test_csv_matches_the_daily_file_format():
    proposal, _ = _check(_raw())
    rows = list(csv.DictReader(io.StringIO(proposal_csv(proposal))))
    assert list(rows[0]) == ["url", "name", "source_display_name", "city_slug", "timezone"]
    assert rows[0] == {
        "url": "https://events.umich.edu/list",
        "name": "Ann Arbor - Happening @ Michigan",
        "source_display_name": "Happening @ Michigan",
        "city_slug": "ann-arbor-mi",
        "timezone": "America/Detroit",
    }


# --- the conversation with Claude ---------------------------------------------


def _response(content, stop_reason="tool_use"):
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=50,
        cache_read_input_tokens=0,
        server_tool_use=SimpleNamespace(web_search_requests=2, web_fetch_requests=3),
    )
    return SimpleNamespace(content=content, stop_reason=stop_reason, usage=usage)


def _submit(raw, block_id="toolu_1"):
    return SimpleNamespace(type="tool_use", name="submit_town", input=raw, id=block_id)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        # Copy the messages: the caller keeps appending to the same list.
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.responses.pop(0)


def test_discovery_returns_the_submitted_town():
    client = FakeClient([_response([_submit(_raw())])])
    proposal = discover_town("Ann Arbor", "Michigan", [], client=client)
    assert proposal.slug == "ann-arbor-mi"
    assert proposal.usage["web_search_requests"] == 2

    call = client.calls[0]
    assert call["model"] == get_settings().town_onboarding_model
    assert {t.get("type", t.get("name")) for t in call["tools"]} == {
        "web_search_20260209",
        "web_fetch_20260209",
        "submit_town",
    }
    assert call["fallbacks"] == "default"
    assert "Ann Arbor, Michigan" in call["messages"][0]["content"]


def test_a_short_proposal_goes_back_to_claude_with_the_problems():
    short = _raw(town=TOWN_PAGES[:1])
    client = FakeClient(
        [_response([_submit(short, "toolu_a")]), _response([_submit(_raw(), "toolu_b")])]
    )
    proposal = discover_town("Ann Arbor", None, [], client=client)
    assert len(proposal.sources) == 8

    feedback = client.calls[1]["messages"][-1]["content"][0]
    assert feedback["type"] == "tool_result"
    assert feedback["tool_use_id"] == "toolu_a"
    assert feedback["is_error"] is True
    assert "at least 3" in feedback["content"]
    assert proposal.usage["input_tokens"] == 200


def test_a_paused_search_is_resumed():
    client = FakeClient(
        [
            _response([SimpleNamespace(type="server_tool_use")], stop_reason="pause_turn"),
            _response([_submit(_raw())]),
        ]
    )
    discover_town("Ann Arbor", None, [], client=client)
    assert len(client.calls) == 2
    # A resume sends the paused turn back with no extra user message.
    assert client.calls[1]["messages"][-1]["role"] == "assistant"


def test_a_refusal_fails_the_town():
    client = FakeClient([_response([], stop_reason="refusal")])
    with pytest.raises(TownOnboardingError):
        discover_town("Ann Arbor", None, [], client=client)


def test_giving_up_after_repeated_short_proposals():
    short = _raw(town=TOWN_PAGES[:1])
    client = FakeClient([_response([_submit(short, f"t{i}")]) for i in range(10)])
    with pytest.raises(TownOnboardingError, match="enough usable sources"):
        discover_town("Ann Arbor", None, [], client=client)


def test_no_key_means_no_call(monkeypatch):
    monkeypatch.setattr(get_settings(), "anthropic_api_key", None)
    with pytest.raises(TownOnboardingNotConfigured):
        discover_town("Ann Arbor")


# --- creating the town and queuing its sources -----------------------------------


def _discover_ok(*_args, **_kwargs) -> TownProposal:
    proposal, _ = _check(_raw())
    return proposal


def test_a_queued_town_creates_the_city_and_a_batch(db_session):
    queue_town(
        db_session, town_name="Ann Arbor", state_or_region="Michigan", universities=[],
        requested_by_user_id=None,
    )
    row = process_next_request(db_session, discover=_discover_ok)
    assert row.status == TOWN_DONE

    city = db_session.query(City).filter_by(slug="ann-arbor-mi").one()
    assert city.timezone == "America/Detroit"
    assert [u.name for u in city.universities] == ["University of Michigan"]
    assert row.city_id == city.id

    batch = db_session.get(OnboardingBatch, row.batch_id)
    assert batch.source_kind == SOURCE_CLAUDE
    jobs = db_session.query(OnboardingJob).filter_by(batch_id=batch.id).all()
    assert len(jobs) == 8
    assert {job.city_id for job in jobs} == {city.id}
    assert "Ann Arbor - The Ark" in {job.submitted_name for job in jobs}
    # Nothing else is waiting.
    assert process_next_request(db_session, discover=_discover_ok) is None


def test_an_existing_town_is_used_as_it_is(db_session, make_city):
    existing = make_city(name="Ann Arbor", slug="annarbor", timezone="America/Detroit")
    queue_town(
        db_session, town_name="Ann Arbor", state_or_region=None, universities=[],
        requested_by_user_id=None,
    )
    row = process_next_request(db_session, discover=_discover_ok)
    assert row.status == TOWN_DONE
    assert row.city_id == existing.id
    assert db_session.query(City).count() == 1
    jobs = db_session.query(OnboardingJob).filter_by(batch_id=row.batch_id).all()
    assert {job.city_id for job in jobs} == {existing.id}


def test_a_failed_search_is_recorded(db_session):
    def fail(*_args, **_kwargs):
        raise TownOnboardingError("Claude did not find enough usable sources.")

    queue_town(
        db_session, town_name="Nowhere", state_or_region=None, universities=[],
        requested_by_user_id=None,
    )
    row = process_next_request(db_session, discover=fail)
    assert row.status == TOWN_FAILED
    assert "enough usable sources" in row.failure_reason
    assert db_session.query(City).count() == 0


# --- admin pages -------------------------------------------------------------------


@pytest.fixture
def admin_client(client, make_super_admin, login):
    make_super_admin(email="towns-root@example.com")
    login("towns-root@example.com")
    return client


def _csrf(client) -> str:
    return client.cookies.get("csrf_token")


def test_page_explains_when_claude_is_not_set_up(admin_client, monkeypatch):
    monkeypatch.setattr(get_settings(), "anthropic_api_key", None)
    resp = admin_client.get("/admin/onboarding/towns")
    assert resp.status_code == 200
    assert "ANTHROPIC_API_KEY" in resp.text

    resp = admin_client.post(
        "/admin/onboarding/towns",
        data={"town_name": "Ann Arbor", "csrf_token": _csrf(admin_client)},
    )
    assert resp.status_code == 422


def test_submitting_a_town_queues_it(admin_client, db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "test-key")
    resp = admin_client.post(
        "/admin/onboarding/towns",
        data={
            "town_name": "Ann Arbor",
            "state_or_region": "Michigan",
            "universities": "University of Michigan\nEastern Michigan University",
            "csrf_token": _csrf(admin_client),
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    row = db_session.query(TownOnboardingRequest).one()
    assert row.status == TOWN_QUEUED
    assert row.universities == ["University of Michigan", "Eastern Michigan University"]
    assert resp.headers["location"] == f"/admin/onboarding/towns/{row.id}"

    page = admin_client.get(resp.headers["location"])
    assert "still searching" in page.text


def test_detail_shows_what_claude_found_and_offers_the_csv(admin_client, db_session):
    queue_town(
        db_session, town_name="Ann Arbor", state_or_region="Michigan", universities=[],
        requested_by_user_id=None,
    )
    row = process_next_request(db_session, discover=_discover_ok)

    page = admin_client.get(f"/admin/onboarding/towns/{row.id}")
    assert page.status_code == 200
    assert "On campus (4)" in page.text
    assert "In town (4)" in page.text
    assert "The Ark" in page.text

    resp = admin_client.get(f"/admin/onboarding/towns/{row.id}/sources.csv")
    assert resp.status_code == 200
    assert resp.text.startswith("url,name,source_display_name,city_slug,timezone")
    assert "ann-arbor-mi.csv" in resp.headers["content-disposition"]

    listing = admin_client.get("/admin/onboarding/towns")
    assert "Ann Arbor" in listing.text


def test_a_failed_town_can_be_retried(admin_client, db_session):
    row = queue_town(
        db_session, town_name="Nowhere", state_or_region=None, universities=[],
        requested_by_user_id=None,
    )
    row.status = TOWN_FAILED
    row.failure_reason = "no sources"
    db_session.commit()

    resp = admin_client.post(
        f"/admin/onboarding/towns/{row.id}/retry",
        data={"csrf_token": _csrf(admin_client)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    db_session.refresh(row)
    assert row.status == TOWN_QUEUED
    assert row.failure_reason is None


def test_registered_users_cannot_reach_the_page(client, make_user, login):
    make_user(email="reader@example.com")
    login("reader@example.com")
    resp = client.get("/admin/onboarding/towns")
    assert resp.status_code in (403, 302, 303)
