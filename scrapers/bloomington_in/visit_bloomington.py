import json
import re
from datetime import datetime
from urllib.parse import urljoin

import httpx
from scrapers.base import BaseScraper, Event

import logging
logger = logging.getLogger(__name__)


class VisitBloomingtonScraper(BaseScraper):
    name = "Visit Bloomington"
    base_url = "https://www.visitbloomington.com/events/"
    api_url = "https://www.visitbloomington.com/includes/rest_v2/plugins_events_events_by_date/find/"

    MONTHS = {
        "january": "01", "february": "02", "march": "03", "april": "04",
        "may": "05", "june": "06", "july": "07", "august": "08",
        "september": "09", "october": "10", "november": "11", "december": "12",
        "jan": "01", "feb": "02", "mar": "03", "apr": "04",
        "jun": "06", "jul": "07", "aug": "08",
        "sep": "09", "sept": "09", "oct": "10", "nov": "11", "dec": "12",
    }

    def _clean(self, text: str) -> str:
        return " ".join(text.split()).strip()

    def _extract_token(self, html: str | None) -> str | None:
        if not html:
            return None

        patterns = [
            r"simpleToken\s*[:=]\s*'([^']+)'",
            r'simpleToken\s*[:=]\s*"([^"]+)"',
            r'"simpleToken"\s*:\s*"([^"]+)"',
        ]
        for pattern in patterns:
            m = re.search(pattern, html)
            if m:
                return m.group(1)
        return None

    def _extract_date_time(self, value) -> tuple[str | None, str | None]:
        date = None
        time = None

        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d"), value.strftime("%H:%M")

        if not value:
            return None, None

        text = str(value).strip()
        if len(text) >= 10:
            date = text[:10]

        tm = re.search(r"\b(\d{1,2}:\d{2})\b", text)
        if tm:
            time = tm.group(1)

        return date, time

    def _extract_venue(self, doc: dict) -> str | None:
        locations = doc.get("locations") or []
        if locations and isinstance(locations, list):
            first = locations[0] or {}
            title = first.get("title")
            if title:
                return self._clean(str(title))

        if doc.get("location"):
            return self._clean(str(doc["location"]))

        if doc.get("listing") and isinstance(doc["listing"], dict):
            listing = doc["listing"]
            title = listing.get("title")
            if title:
                return self._clean(str(title))

        return None

    def _extract_image_url(self, doc: dict) -> str | None:
        media_raw = doc.get("media_raw") or []
        if isinstance(media_raw, list) and media_raw:
            first = media_raw[0] or {}
            return first.get("mediaurl") or first.get("url")

        if doc.get("image_url"):
            return doc["image_url"]

        return None

    def _build_options(self, skip: int, limit: int = 100) -> dict:
        return {
            "limit": limit,
            "skip": skip,
            "count": True,
            "castDocs": False,
            "fields": {
                "_id": 1,
                "location": 1,
                "date": 1,
                "startDate": 1,
                "endDate": 1,
                "recurrence": 1,
                "recurType": 1,
                "latitude": 1,
                "longitude": 1,
                "media_raw": 1,
                "recid": 1,
                "title": 1,
                "url": 1,
                "categories": 1,
                "accountId": 1,
                "city": 1,
                "region": 1,
                "listing": 1,
            },
        }

    def _doc_to_event(self, doc: dict) -> Event | None:
        title = self._clean(str(doc.get("title") or ""))
        url = str(doc.get("url") or "").strip()
        if not title or not url:
            return None

        if url.startswith("/"):
            url = urljoin("https://www.visitbloomington.com", url)

        date, time = self._extract_date_time(doc.get("start_date") or doc.get("startDate") or doc.get("date"))
        if not date:
            date, _ = self._extract_date_time(doc.get("date"))

        venue = self._extract_venue(doc)
        image_url = self._extract_image_url(doc)

        city_field = doc.get("city") or "Bloomington"
        region = doc.get("region") or "IN"
        address = f"{city_field}, {region}".strip(", ")

        return Event(
            title=title,
            url=url,
            source=self.name,
            date=date,
            time=time,
            venue=venue,
            address=address,
            image_url=image_url,
            category="Community",
            city=self.city,
        )

    async def scrape(self) -> list[Event]:
        events: list[Event] = []
        seen_urls: set[str] = set()

        page_html = await self.fetch(self.base_url)
        token = self._extract_token(page_html)

        async with httpx.AsyncClient(
            headers=self.HEADERS,
            follow_redirects=True,
            timeout=25,
        ) as client:
            skip = 0
            limit = 100

            for _ in range(20):
                payload = {
                    "filter": {},
                    "options": self._build_options(skip=skip, limit=limit),
                }

                params = {"json": json.dumps(payload)}
                if token:
                    params["token"] = token

                try:
                    response = await client.get(self.api_url, params=params)
                    response.raise_for_status()
                    data = response.json()
                except Exception as e:
                    logger.error(f"[{self.name}] API request failed: {e}")
                    break

                docs_container = data.get("docs", {})
                docs = docs_container.get("docs", [])
                if not docs:
                    break

                for doc in docs:
                    event = self._doc_to_event(doc)
                    if not event or event.url in seen_urls:
                        continue
                    seen_urls.add(event.url)
                    events.append(event)

                if len(docs) < limit:
                    break

                skip += limit

        return events
