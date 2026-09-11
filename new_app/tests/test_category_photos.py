"""Category photo placeholder pool: fetch/store parsing and deterministic pick."""

from __future__ import annotations

import httpx
import pytest

import app.services.category_photos as cp
from app.models.category_photo import CategoryPhoto


@pytest.fixture(autouse=True)
def _clear_pool_cache():
    cp.reload()
    yield
    cp.reload()


def _mock_unsplash(monkeypatch, results_by_query):
    """Patch the httpx client the service builds so each category query returns a
    distinct canned result set, keyed by the search term."""

    def handler(request: httpx.Request) -> httpx.Response:
        query = dict(request.url.params).get("query", "")
        return httpx.Response(200, json={"results": results_by_query.get(query, [])})

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        return real_client(transport=httpx.MockTransport(handler), timeout=5)

    monkeypatch.setattr(cp.httpx, "Client", fake_client)


def _photo(pid: str, name: str):
    return {
        "id": pid,
        "urls": {"regular": f"https://img/{pid}.jpg", "small": f"https://img/{pid}-s.jpg"},
        "user": {"name": name, "links": {"html": f"https://unsplash.com/@{name}"}},
        "links": {"html": f"https://unsplash.com/photos/{pid}"},
    }


def test_fetch_stores_photos_with_attribution(db_session, monkeypatch):
    _mock_unsplash(
        monkeypatch,
        {
            cp.CATEGORY_QUERIES["music"]: [_photo("m1", "Jane"), _photo("m2", "Bob")],
            cp.CATEGORY_QUERIES["sports"]: [_photo("s1", "Amy")],
        },
    )
    stored = cp.fetch_and_store(db_session, "FAKE", per_category=5, app_name="city-events")

    assert stored["music"] == 2
    assert stored["sports"] == 1
    row = db_session.query(CategoryPhoto).filter_by(unsplash_id="m1").one()
    assert row.category_slug == "music"
    assert row.image_url == "https://img/m1.jpg"
    # Unsplash referral UTM is added to the credit links.
    assert "utm_source=city-events" in row.photographer_url
    assert "utm_medium=referral" in row.source_url


def test_a_photo_is_never_stored_under_two_categories(db_session, monkeypatch):
    shared = _photo("dup", "Sam")
    _mock_unsplash(
        monkeypatch,
        {cp.CATEGORY_QUERIES["music"]: [shared], cp.CATEGORY_QUERIES["sports"]: [shared]},
    )
    cp.fetch_and_store(db_session, "FAKE", per_category=5)
    assert db_session.query(CategoryPhoto).filter_by(unsplash_id="dup").count() == 1


def test_photo_for_is_deterministic_and_falls_back_to_other(db_session, monkeypatch):
    _mock_unsplash(
        monkeypatch,
        {
            cp.CATEGORY_QUERIES["music"]: [_photo("m1", "A"), _photo("m2", "B")],
            cp.CATEGORY_QUERIES["other"]: [_photo("o1", "C")],
        },
    )
    cp.fetch_and_store(db_session, "FAKE", per_category=5)
    monkeypatch.setattr(cp, "_CACHE_TTL", 1e9)  # keep the freshly loaded pool

    # Same event id always maps to the same photo in its category.
    first = cp.photo_for("music", 4)
    assert first is not None
    assert cp.photo_for("music", 4).image_url == first.image_url
    # A category with no photos of its own falls back to the 'other' pool.
    fallback = cp.photo_for("religious", 7)
    assert fallback is not None and fallback.image_url == "https://img/o1.jpg"
