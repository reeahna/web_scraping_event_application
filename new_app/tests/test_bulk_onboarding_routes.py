"""Admin routes for bulk onboarding: submission, batch list/detail, job detail."""

from __future__ import annotations

import io

import httpx
import pytest

from app.core.permissions import REGISTERED_USER
from app.models.event import Event
from app.models.onboarding_batch import OnboardingBatch
from app.models.website import Website
from tests.extraction_helpers import load_fixture, patched_http_fetch

GOOD_URL = "https://hall.example.org/events"


def _csrf(client) -> str:
    return client.cookies.get("csrf_token")


def _handler(routes: dict[str, str]):
    def handler(request: httpx.Request) -> httpx.Response:
        body = routes.get(str(request.url))
        if body is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, text=body, headers={"content-type": "text/html"})

    return handler


@pytest.fixture
def admin_client(client, make_super_admin, login):
    make_super_admin(email="onboard-root@example.com", password="root-pass-1234")
    login("onboard-root@example.com", "root-pass-1234")
    return client


@pytest.fixture
def city(make_city):
    return make_city(name="Route City", slug="route-city", timezone="UTC")


def _submit(admin_client, city, **overrides):
    data = {
        "urls": GOOD_URL,
        "city_id": str(city.id),
        "default_timezone": "",
        "csrf_token": _csrf(admin_client),
    }
    data.update(overrides)
    files = data.pop("files", None)
    routes = {GOOD_URL: load_fixture("inference_cards_weekday_date.html")}
    with patched_http_fetch(_handler(routes)):
        return admin_client.post(
            "/admin/websites/onboard", data=data, files=files, follow_redirects=False
        )


# --- submission --------------------------------------------------------------


def test_submission_page_loads(admin_client, city):
    resp = admin_client.get("/admin/websites/onboard")
    assert resp.status_code == 200
    assert "Onboard event sources" in resp.text
    assert "Route City" in resp.text
    # The main path never asks for extraction configuration.
    assert "field_selectors" not in resp.text
    assert "raw_json" not in resp.text


def test_single_url_submission_creates_a_batch_and_website(admin_client, city, db_session):
    resp = _submit(admin_client, city)
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/admin/onboarding/batches/")

    batch = db_session.query(OnboardingBatch).one()
    assert batch.valid_count == 1
    assert batch.jobs[0].status == "ready_for_approval"
    assert db_session.query(Website).count() == 1
    assert db_session.query(Event).count() == 0


def test_multiline_submission_queues_each_url(admin_client, city, db_session):
    resp = _submit(admin_client, city, urls=f"{GOOD_URL}\n\nhttps://other.example.org/events\n")
    assert resp.status_code == 303
    batch = db_session.query(OnboardingBatch).one()
    assert batch.valid_count == 2


def test_csv_submission_is_accepted(admin_client, city, db_session):
    csv_bytes = b"url,name\nhttps://hall.example.org/events,Supplied Name\n"
    resp = _submit(
        admin_client,
        city,
        urls="",
        files={"csv_file": ("sources.csv", io.BytesIO(csv_bytes), "text/csv")},
    )
    assert resp.status_code == 303
    batch = db_session.query(OnboardingBatch).one()
    assert batch.source_kind == "csv"
    assert batch.jobs[0].submitted_name == "Supplied Name"
    website = db_session.get(Website, batch.jobs[0].website_id)
    assert website.name == "Supplied Name"


def test_malformed_csv_is_rejected_with_a_message(admin_client, city, db_session):
    resp = _submit(
        admin_client,
        city,
        urls="",
        files={
            "csv_file": (
                "sources.csv",
                io.BytesIO(b"website\nhttps://a.example.org\n"),
                "text/csv",
            )
        },
    )
    assert resp.status_code == 422
    assert "url" in resp.text
    assert db_session.query(OnboardingBatch).count() == 0


def test_submission_without_a_city_is_rejected(admin_client, city, db_session):
    resp = _submit(admin_client, city, city_id="")
    assert resp.status_code == 422
    assert db_session.query(OnboardingBatch).count() == 0


def test_submission_with_no_valid_urls_is_rejected(admin_client, city, db_session):
    resp = _submit(admin_client, city, urls="http://localhost/admin")
    assert resp.status_code == 422
    assert db_session.query(OnboardingBatch).count() == 0


def test_submission_requires_csrf(admin_client, city, db_session):
    resp = admin_client.post(
        "/admin/websites/onboard",
        data={"urls": GOOD_URL, "city_id": str(city.id), "csrf_token": "wrong"},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    assert db_session.query(OnboardingBatch).count() == 0


def test_submission_requires_the_create_permission(client, make_user, login, city):
    make_user(email="plain@example.com", password="user-pass-1234", role_name=REGISTERED_USER)
    login("plain@example.com", "user-pass-1234")
    resp = client.get("/admin/websites/onboard", follow_redirects=False)
    assert resp.status_code in (302, 303, 403)


# --- batch and job screens ---------------------------------------------------


def test_batch_list_and_detail_load(admin_client, city, db_session):
    _submit(admin_client, city)
    batch = db_session.query(OnboardingBatch).one()

    listing = admin_client.get("/admin/onboarding/batches")
    assert listing.status_code == 200
    assert "Onboarding batches" in listing.text
    assert "Route City" in listing.text

    detail = admin_client.get(f"/admin/onboarding/batches/{batch.id}")
    assert detail.status_code == 200
    assert GOOD_URL in detail.text
    assert "generic_html_cards" in detail.text
    assert "ready for approval" in detail.text
    # The review link goes to the Phase 8B onboarding page.
    assert f"/admin/websites/{batch.jobs[0].website_id}/onboarding" in detail.text


def test_job_detail_loads_with_inferred_metadata_and_no_configuration_json(
    admin_client, city, db_session
):
    _submit(admin_client, city)
    job = db_session.query(OnboardingBatch).one().jobs[0]

    resp = admin_client.get(f"/admin/onboarding/jobs/{job.id}")
    assert resp.status_code == 200
    assert "Inferred site metadata" in resp.text
    assert "hall.example.org" in resp.text
    assert "Review and approve" in resp.text
    assert "<textarea" not in resp.text


def test_batch_and_job_screens_require_view_permission(client, make_user, login):
    make_user(email="plain2@example.com", password="user-pass-1234", role_name=REGISTERED_USER)
    login("plain2@example.com", "user-pass-1234")
    resp = client.get("/admin/onboarding/batches", follow_redirects=False)
    assert resp.status_code in (302, 303, 403)


def test_process_route_requires_the_test_permission(client, make_user, login, city):
    make_user(email="plain3@example.com", password="user-pass-1234", role_name=REGISTERED_USER)
    login("plain3@example.com", "user-pass-1234")
    resp = client.post(
        "/admin/onboarding/batches/1/process",
        data={"csrf_token": client.cookies.get("csrf_token")},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 303, 403)
