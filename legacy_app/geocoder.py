import asyncio
import logging

import httpx
from database import get_conn, update_geocode

logger = logging.getLogger(__name__)

_NOMINATIM = "https://nominatim.openstreetmap.org/search"
_HEADERS = {"User-Agent": "CityEventsAggregator/1.0 (ahnabrah@iu.edu)"}

# In-memory cache so repeated addresses within one run don't burn extra requests
_cache: dict[str, tuple[float, float] | None] = {}


async def _geocode(query: str) -> tuple[float, float] | None:
    if query in _cache:
        return _cache[query]
    try:
        async with httpx.AsyncClient(headers=_HEADERS, timeout=10) as client:
            resp = await client.get(
                _NOMINATIM,
                params={"q": query, "format": "json", "limit": 1},
            )
            resp.raise_for_status()
            results = resp.json()
            if results:
                result: tuple[float, float] = (float(results[0]["lat"]), float(results[0]["lon"]))
                _cache[query] = result
                return result
    except Exception as e:
        logger.debug(f"Geocode failed for '{query}': {e}")
    _cache[query] = None
    return None


def _build_query(event: dict) -> str | None:
    venue = (event.get("venue") or "").strip()
    address = (event.get("address") or "").strip()
    city = (event.get("city") or "").strip()
    # A street address is useful if it contains a digit (house number)
    has_street = any(c.isdigit() for c in address)
    if has_street:
        return f"{venue}, {address}" if venue else address
    if venue:
        # venue + city is worth trying (e.g. "Frank Southern Center, Bloomington, IN")
        return f"{venue}, {city}" if city else venue
    # City-only strings like "Bloomington, IN" aren't specific enough
    return None


async def geocode_new_events(limit: int = 150) -> int:
    """Geocode up to `limit` events that have venue/address but no coordinates yet."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT url, venue, address, city FROM events
               WHERE lat IS NULL
               AND (TRIM(COALESCE(venue, '')) != '' OR TRIM(COALESCE(address, '')) != '')
               LIMIT ?""",
            (limit,),
        ).fetchall()

    if not rows:
        return 0

    logger.info(f"[Geocoder] Starting geocoding for {len(rows)} events")
    count = 0
    for row in rows:
        query = _build_query(dict(row))
        if not query:
            continue
        result = await _geocode(query)
        if result:
            update_geocode(row["url"], result[0], result[1])
            count += 1
        await asyncio.sleep(1.1)  # Nominatim: max 1 req/sec

    logger.info(f"[Geocoder] Geocoded {count}/{len(rows)} events")
    return count
