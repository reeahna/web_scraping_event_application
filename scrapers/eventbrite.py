# scrapers/eventbrite.py
import json
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from scrapers.base import BaseScraper, Event


class EventbriteScraper(BaseScraper):
    name = "Eventbrite"
    base_url = "https://www.eventbrite.com/d/in--bloomington/bloomington/"

    async def scrape(self) -> list[Event]:
        events: list[Event] = []
        seen: set[tuple[str, str]] = set()

        for page_num in range(1, 6):
            url = self.base_url if page_num == 1 else f"{self.base_url}?page={page_num}"
            html = await self.render(url)
            if not html:
                continue

            soup = BeautifulSoup(html, "html.parser")
            added_before = len(events)

            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    data = json.loads(script.string or "")
                    items = data if isinstance(data, list) else [data]
                except (json.JSONDecodeError, TypeError):
                    continue

                for item in items:
                    if not isinstance(item, dict) or item.get("@type") != "Event":
                        continue

                    title = (item.get("name") or "").strip()
                    url = (item.get("url") or "").strip()
                    if not title or not url:
                        continue

                    url = urljoin("https://www.eventbrite.com", url)
                    key = (title, url)
                    if key in seen:
                        continue
                    seen.add(key)

                    start_date = item.get("startDate", "") or ""
                    date = start_date[:10] if start_date else None
                    time = start_date[11:16] if len(start_date) > 10 else None

                    description = (item.get("description") or "").strip()

                    image_url = item.get("image", "")
                    if isinstance(image_url, list):
                        image_url = image_url[0] if image_url else ""
                    elif not isinstance(image_url, str):
                        image_url = ""

                    location = item.get("location", {}) or {}
                    venue = location.get("name", "")

                    address = None
                    address_obj = location.get("address", {})
                    if isinstance(address_obj, dict):
                        parts = [
                            address_obj.get("streetAddress", ""),
                            address_obj.get("addressLocality", ""),
                            address_obj.get("addressRegion", ""),
                        ]
                        address = ", ".join(p for p in parts if p) or None
                    elif address_obj:
                        address = str(address_obj)

                    events.append(
                        Event(
                            title=title,
                            url=url,
                            source=self.name,
                            description=description[:500] if description else None,
                            date=date,
                            time=time,
                            venue=venue,
                            address=address,
                            image_url=image_url or None,
                            category="Eventbrite",
                        )
                    )

            if page_num > 1 and len(events) == added_before:
                break

        return events