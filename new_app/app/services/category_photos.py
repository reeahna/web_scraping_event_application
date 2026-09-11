"""Category photo placeholders.

Builds a small pool of Unsplash photos per event category (populated by
scripts/fetch_category_photos.py using the deployment's Unsplash Access Key),
and hands the templates a relevant photo for an event that has no image of its
own. The choice is keyed to the event id, so an event keeps the same photo
across reloads while the grid still gets variety. Everything degrades to the
gradient placeholder when the pool is empty, so the site never depends on it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session

from app.models.category_photo import CategoryPhoto

# A good, safe Unsplash search term for each seeded category slug. Never a
# per-site value; purely the category taxonomy.
CATEGORY_QUERIES: dict[str, str] = {
    "arts-and-culture": "art exhibition",
    "business": "business conference",
    "community": "community festival",
    "education": "lecture hall",
    "family": "family fun day",
    "food-and-drink": "food festival",
    "government": "town hall meeting",
    "health-and-wellness": "yoga wellness",
    "music": "live concert",
    "nightlife": "nightclub party",
    "other": "crowd event",
    "outdoors": "outdoor park festival",
    "religious": "church interior",
    "sports": "sports stadium crowd",
}

_UNSPLASH_SEARCH = "https://api.unsplash.com/search/photos"


@dataclass(frozen=True)
class PhotoCredit:
    image_url: str
    thumb_url: str | None
    photographer: str
    photographer_url: str
    source_url: str


# Process-wide cache of the pool, grouped by category slug. The pool only
# changes when the fetch script runs (a separate process), so it is cached and
# refreshed at most every _CACHE_TTL seconds: that keeps the template lookup a
# pure dict access (no per-event query) while still picking up freshly fetched
# photos without a manual restart.
_pool: dict[str, list[PhotoCredit]] | None = None
_loaded_at: float = 0.0
_CACHE_TTL = 600.0


def reload() -> None:
    global _pool
    _pool = None


def _load() -> dict[str, list[PhotoCredit]]:
    global _pool, _loaded_at
    if _pool is None or (time.monotonic() - _loaded_at) > _CACHE_TTL:
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            grouped: dict[str, list[PhotoCredit]] = {}
            for row in db.query(CategoryPhoto).order_by(CategoryPhoto.id).all():
                grouped.setdefault(row.category_slug, []).append(
                    PhotoCredit(
                        image_url=row.image_url,
                        thumb_url=row.thumb_url,
                        photographer=row.photographer,
                        photographer_url=row.photographer_url,
                        source_url=row.source_url,
                    )
                )
        finally:
            db.close()
        _pool = grouped
        _loaded_at = time.monotonic()
    return _pool


def photo_for(category_slug: str | None, event_id: int) -> PhotoCredit | None:
    """A category-relevant photo for an imageless event, chosen deterministically
    by event id. Falls back to the generic 'other' pool for a category with no
    photos, then to None (the caller shows the gradient)."""
    pool = _load()
    for slug in (category_slug or "other", "other"):
        photos = pool.get(slug)
        if photos:
            return photos[event_id % len(photos)]
    return None


def _credit_link(url: str, app_name: str) -> str:
    # Unsplash asks that photographer/source links carry a referral UTM.
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}utm_source={app_name}&utm_medium=referral"


def fetch_and_store(
    db: Session, access_key: str, *, per_category: int = 10, app_name: str = "city-events"
) -> dict[str, int]:
    """Refresh the whole pool from Unsplash: one search per category (well under
    the demo rate limit), replacing all stored photos. Returns a per-category
    count of photos stored. Requires only the Access Key (Client-ID)."""
    headers = {"Authorization": f"Client-ID {access_key}", "Accept-Version": "v1"}
    db.query(CategoryPhoto).delete()
    seen: set[str] = set()
    stored: dict[str, int] = {}
    with httpx.Client(timeout=30, headers=headers) as client:
        for slug, query in CATEGORY_QUERIES.items():
            count = 0
            response = client.get(
                _UNSPLASH_SEARCH,
                params={
                    "query": query,
                    "per_page": per_category,
                    "orientation": "landscape",
                    "content_filter": "high",
                },
            )
            response.raise_for_status()
            for result in response.json().get("results", []):
                photo_id = result.get("id")
                urls = result.get("urls") or {}
                user = result.get("user") or {}
                links = result.get("links") or {}
                image_url = urls.get("regular")
                if not photo_id or not image_url or photo_id in seen:
                    continue
                seen.add(photo_id)
                db.add(
                    CategoryPhoto(
                        category_slug=slug,
                        unsplash_id=photo_id,
                        image_url=image_url,
                        thumb_url=urls.get("small"),
                        photographer=user.get("name") or "Unsplash",
                        photographer_url=_credit_link(
                            (user.get("links") or {}).get("html", "https://unsplash.com"),
                            app_name,
                        ),
                        source_url=_credit_link(
                            links.get("html", "https://unsplash.com"), app_name
                        ),
                    )
                )
                count += 1
            stored[slug] = count
    db.commit()
    reload()
    return stored
