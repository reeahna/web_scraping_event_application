import asyncio
import logging
import re
import time
from datetime import datetime

import httpx
from scrapers.base import BaseScraper, Event

logger = logging.getLogger(__name__)

ALGOLIA_APP_ID  = "EYQHJ2IY2M"
ALGOLIA_API_KEY = "c6d5977cb5cd80c09abfd2a7e5d9e88b"
ALGOLIA_INDEX   = "prod-discover-lehigh-valley-listings"
ALGOLIA_URL     = f"https://{ALGOLIA_APP_ID}-dsn.algolia.net/1/indexes/{ALGOLIA_INDEX}/query"
BASE_URL        = "https://www.discoverlehighvalley.com"

CATEGORY_MAP = {
    "amusement & entertainment": "Arts & Performance",
    "arts & culture": "Arts & Performance",
    "nightlife": "Arts & Performance",
    "music & concerts": "Music",
    "music": "Music",
    "festivals & events": "Community",
    "tours & sightseeing": "Community",
    "shopping": "Community",
    "food & drink": "Food & Drink",
    "restaurants": "Food & Drink",
    "sports & recreation": "Sports & Recreation",
    "outdoor recreation": "Sports & Recreation",
    "health & wellness": "Health & Wellness",
    "family & kids": "Family & Youth",
    "education": "Lectures & Education",
    "business": "Business & Career",
    "religion & spirituality": "Religion & Spirituality",
}

_BETHLEHEM_ZIPS = {"18015", "18016", "18017", "18018"}
_HTML_TAG_RE = re.compile(r"<[^>]+>")


class BethlehemScraper(BaseScraper):
    name = "Discover Lehigh Valley - Bethlehem"
    city = "Bethlehem, PA"

    def _is_bethlehem(self, hit: dict) -> bool:
        """Keep only hits associated with Bethlehem, PA."""
        # URI-based check (e.g. /cities-towns/bethlehem/events/...)
        uri = hit.get("uri") or ""
        if "bethlehem" in uri.lower():
            return True
        # Address array: ["Venue", "123 Main St", "Bethlehem, PA 18015"]
        for part in (hit.get("address") or []):
            part_lower = part.lower()
            if "bethlehem" in part_lower:
                return True
            for z in _BETHLEHEM_ZIPS:
                if z in part:
                    return True
        return False

    def _parse_hit(self, hit: dict) -> Event | None:
        title = (hit.get("title") or "").strip()
        uri   = (hit.get("uri")   or "").strip()
        if not title or not uri:
            return None

        url = f"{BASE_URL}{uri}"

        start_ts = hit.get("startDate")
        end_ts   = hit.get("endDate")
        start_dt = datetime.fromtimestamp(start_ts) if start_ts else None
        end_dt   = datetime.fromtimestamp(end_ts)   if end_ts   else None

        date     = start_dt.strftime("%Y-%m-%d") if start_dt else None
        end_date = (
            end_dt.strftime("%Y-%m-%d")
            if (end_dt and hit.get("isMultiDay"))
            else None
        )
        if end_date == date:
            end_date = None
        time_str = (
            None if hit.get("isAllDay")
            else (start_dt.strftime("%I:%M %p").lstrip("0") if start_dt else None)
        )

        address_parts = hit.get("address") or []
        venue   = hit.get("locationName") or (address_parts[0] if address_parts else None)
        address = ", ".join(address_parts[1:]) if len(address_parts) > 1 else None

        cats     = hit.get("eventCategories") or []
        raw_cat  = cats[0].lower() if cats else ""
        category = CATEGORY_MAP.get(raw_cat, "Community")

        raw_desc = _HTML_TAG_RE.sub(" ", hit.get("content") or "")
        description = " ".join(raw_desc.split())[:500] or None

        return Event(
            title=title,
            url=url,
            source=self.name,
            description=description,
            date=date,
            end_date=end_date,
            time=time_str,
            venue=venue,
            address=address,
            image_url=hit.get("primaryImageUrl") or None,
            category=category,
            city=self.city,
        )

    async def _fetch_page(self, client: httpx.AsyncClient, page: int, now_ts: int) -> dict:
        body = {
            "params": (
                f"hitsPerPage=144&page={page}"
                f"&facetFilters=%5B%5B%22sectionName%3AEvents%22%5D%5D"
                f"&numericFilters=endDate%3E{now_ts}"
                f"&attributesToRetrieve=title%2Curi%2CstartDate%2CendDate"
                f"%2CisAllDay%2CisMultiDay%2ClocationName%2Caddress"
                f"%2CeventCategories%2CprimaryImageUrl%2Ccontent"
            )
        }
        resp = await client.post(ALGOLIA_URL, json=body, timeout=20)
        resp.raise_for_status()
        return resp.json()

    async def scrape(self) -> list[Event]:
        now_ts = int(time.time())
        algolia_headers = {
            "X-Algolia-API-Key": ALGOLIA_API_KEY,
            "X-Algolia-Application-ID": ALGOLIA_APP_ID,
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(headers=algolia_headers) as client:
            try:
                first = await self._fetch_page(client, 0, now_ts)
            except Exception as e:
                logger.error(f"[{self.name}] Algolia request failed: {e}")
                return []

            total_pages = min(first.get("nbPages", 1), 7)  # Algolia caps at 1000 accessible hits
            all_hits = list(first.get("hits", []))
            logger.info(f"[{self.name}] Total pages: {total_pages}, hits page 0: {len(all_hits)}")

            if total_pages > 1:
                results = await asyncio.gather(
                    *[self._fetch_page(client, p, now_ts) for p in range(1, total_pages)],
                    return_exceptions=True,
                )
                for r in results:
                    if isinstance(r, Exception):
                        logger.warning(f"[{self.name}] Page failed: {r}")
                    else:
                        all_hits.extend(r.get("hits", []))

        logger.info(f"[{self.name}] Total hits from Algolia: {len(all_hits)}")

        events: list[Event] = []
        for hit in all_hits:
            if not self._is_bethlehem(hit):
                continue
            ev = self._parse_hit(hit)
            if ev:
                events.append(ev)

        logger.info(f"[{self.name}] Bethlehem events after filter: {len(events)}")
        return events
