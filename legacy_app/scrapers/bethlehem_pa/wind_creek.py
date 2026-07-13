import logging
import re
from datetime import datetime

import httpx
from scrapers.base import BaseScraper, Event

logger = logging.getLogger(__name__)

_API = "https://www.windcreekeventcenter.com/wp-json/wp/v2/sands_events"
# Matches "on October 10, 2026" or "on June 5, 2026" in Yoast description
_DATE_RE = re.compile(r"\bon\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})", re.IGNORECASE)

_CATEGORY_MAP = {
    "country": "Music",
    "pop": "Music",
    "rock": "Music",
    "hip-hop": "Music",
    "hip_hop": "Music",
    "rnb": "Music",
    "r-b": "Music",
    "latin": "Music",
    "comedy": "Arts & Performance",
    "family": "Family & Youth",
    "sports": "Sports & Recreation",
}


class WindCreekScraper(BaseScraper):
    name = "Wind Creek Event Center"
    city = "Bethlehem, PA"

    def _parse_event_date(self, description: str) -> str | None:
        m = _DATE_RE.search(description)
        if not m:
            return None
        try:
            return datetime.strptime(m.group(1).strip(), "%B %d, %Y").strftime("%Y-%m-%d")
        except ValueError:
            return None

    def _parse_category(self, class_list: list[str]) -> str:
        for cls in class_list:
            if cls.startswith("sands_event_categories-"):
                slug = cls[len("sands_event_categories-"):].lower().replace("_", "-")
                return _CATEGORY_MAP.get(slug, "Music")
        return "Music"

    async def scrape(self) -> list[Event]:
        events: list[Event] = []
        async with httpx.AsyncClient(headers=self.HEADERS, follow_redirects=True, timeout=20) as client:
            page = 1
            while True:
                try:
                    resp = await client.get(
                        _API,
                        params={
                            "per_page": 100,
                            "page": page,
                            "_fields": "id,title,link,class_list,yoast_head_json",
                        },
                    )
                    resp.raise_for_status()
                except Exception as e:
                    logger.error(f"[{self.name}] API request failed (page {page}): {e}")
                    break

                items = resp.json()
                if not items:
                    break

                for item in items:
                    title = (item.get("title") or {}).get("rendered", "").strip()
                    if not title:
                        continue
                    url = (item.get("link") or "").strip()
                    if not url:
                        continue

                    yoast = item.get("yoast_head_json") or {}
                    description = (yoast.get("og_description") or "").strip()
                    date = self._parse_event_date(description)

                    og_images = yoast.get("og_image") or []
                    image_url = og_images[0].get("url") if og_images else None

                    category = self._parse_category(item.get("class_list") or [])

                    events.append(Event(
                        title=title,
                        url=url,
                        source=self.name,
                        description=description[:500] or None,
                        date=date,
                        venue="Wind Creek Event Center",
                        address="77 Wind Creek Blvd, Bethlehem, PA 18015",
                        image_url=image_url,
                        category=category,
                        city=self.city,
                    ))

                total_pages = int(resp.headers.get("X-WP-TotalPages", 1))
                if page >= total_pages:
                    break
                page += 1

        logger.info(f"[{self.name}] Found {len(events)} events")
        return events
