import logging
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from scrapers.base import BaseScraper, Event

logger = logging.getLogger(__name__)


class TheEventsCalendarScraper(BaseScraper):
    """
    Scraper for any site running The Events Calendar WordPress plugin.
    Configure via constructor — no subclassing needed.
    """

    def __init__(self, name: str, base_url: str, city: str, max_pages: int = 10):
        self.name = name
        self.base_url = base_url
        self.city = city
        self._max_pages = max_pages

    def _clean(self, text: str | None) -> str:
        return " ".join(str(text or "").split()).strip()

    def _parse_date_from_abbr(self, abbr) -> str | None:
        if not abbr:
            return None
        # abbr uses title attr; time uses datetime attr
        value = abbr.get("title") or abbr.get("datetime") or ""
        if value and len(value) >= 10:
            return value[:10]
        return None

    def _parse_time_from_abbr(self, abbr) -> str | None:
        if not abbr:
            return None
        value = abbr.get("title") or abbr.get("datetime") or ""
        if value and len(value) >= 16:
            return value[11:16]
        text = abbr.get_text(strip=True)
        m = re.search(r"\d{1,2}:\d{2}\s*[ap]m", text, re.IGNORECASE)
        return m.group() if m else None

    def _infer_category(self, title: str, description: str) -> str:
        text = (title + " " + description).lower()
        if any(k in text for k in ("concert", "music", "band", "jazz", "choir", "orchestra", "recital", "singer")):
            return "Music"
        if any(k in text for k in ("art", "theater", "theatre", "dance", "film", "gallery", "exhibit", "comedy", "screening", "poetry")):
            return "Arts & Performance"
        if any(k in text for k in ("food", "drink", "wine", "beer", "tasting", "dinner", "brunch", "culinary")):
            return "Food & Drink"
        if any(k in text for k in ("festival", "fair", "market", "parade", "community", "fundraiser", "volunteer")):
            return "Community"
        if any(k in text for k in ("sport", "run", "race", "fitness", "yoga", "walk", "hike", "tournament")):
            return "Sports & Recreation"
        if any(k in text for k in ("lecture", "talk", "seminar", "workshop", "class", "training", "education")):
            return "Lectures & Education"
        if any(k in text for k in ("family", "kids", "children", "youth", "camp", "storytime")):
            return "Family & Youth"
        if any(k in text for k in ("spiritual", "faith", "prayer", "church", "religious")):
            return "Religion & Spirituality"
        return "Community"

    def _parse_articles(self, html: str) -> list[Event]:
        soup = BeautifulSoup(html, "html.parser")
        events: list[Event] = []

        # TEC v1/v2/v3 article wrappers
        articles = soup.select(
            "article.type-tribe_events, article.tribe-event, "
            "li.tribe-events-calendar-list__event-row, "
            "div.tribe-events-calendar-list__event"
        )

        for article in articles:
            # Title + URL — try multiple TEC versions then generic heading fallback
            title_a = (
                article.select_one("h2.tribe-events-list-event-title a")
                or article.select_one("h3.tribe-events-calendar-list__event-title a")
                or article.select_one("a.tribe-event-url")
                or article.select_one(".tribe-event-url a")
                or article.select_one("a.tribe-event__title-link")
                or article.select_one(".tribe-event__title a")
                or article.select_one(".tribe-events-calendar-list__event-title a")
                or article.select_one("h1 a, h2 a, h3 a, h4 a")
            )
            if not title_a:
                continue

            title = self._clean(title_a.get_text())
            url = title_a.get("href", "").strip()
            if not title or not url:
                continue
            if not url.startswith("http"):
                url = urljoin(self.base_url, url)

            # Dates — cover all TEC versions; time[datetime] is the v3 fallback
            start_abbr = article.select_one(
                "abbr.tribe-events-start-datetime, "
                "abbr.tribe-events-abbr.tribe-events-start-datetime, "
                "time.tribe-events-calendar-list__event-date-tag-datetime, "
                "time[datetime]"
            )
            end_abbr = article.select_one(
                "abbr.tribe-events-end-datetime, "
                "abbr.tribe-events-abbr.tribe-events-end-datetime"
            )
            date = self._parse_date_from_abbr(start_abbr)
            end_date = self._parse_date_from_abbr(end_abbr)
            time_str = self._parse_time_from_abbr(start_abbr)
            if end_date == date:
                end_date = None

            # Image — lazy-loaded images often have data-src instead of src
            img = article.select_one(
                ".tribe-events-event-image img, "
                ".tribe-event__image img, "
                ".tribe-events-calendar-list__event-image img, "
                ".tribe-event-featured-image img"
            )
            image_url = None
            if img:
                src = img.get("src") or ""
                if src.startswith("data:"):
                    src = img.get("data-src") or img.get("data-lazy-src") or ""
                image_url = src or None

            # Venue
            venue_a = article.select_one(".tribe-venue a, .tribe-events-calendar-list__event-venue a")
            venue = self._clean(venue_a.get_text()) if venue_a else None

            addr_parts = []
            for sel in (".tribe-street-address", ".tribe-city", ".tribe-stateprovince", ".tribe-zip"):
                el = article.select_one(sel)
                if el:
                    addr_parts.append(self._clean(el.get_text()))
            address = ", ".join(p for p in addr_parts if p) or None

            # Description
            desc_el = article.select_one(
                ".tribe-events-list-event-description p, "
                ".tribe-event__description p"
            )
            description = self._clean(desc_el.get_text())[:500] if desc_el else None

            events.append(Event(
                title=title,
                url=url,
                source=self.name,
                description=description,
                date=date,
                end_date=end_date,
                time=time_str,
                venue=venue,
                address=address,
                image_url=image_url,
                category=self._infer_category(title, description or ""),
                city=self.city,
            ))

        return events

    def _page_url(self, n: int) -> str:
        base = self.base_url.rstrip("/")
        return f"{base}/page/{n}/"

    async def scrape(self) -> list[Event]:
        all_events: list[Event] = []
        seen_urls: set[str] = set()

        for page_num in range(1, self._max_pages + 1):
            url = self.base_url if page_num == 1 else self._page_url(page_num)
            # wait_until="load" avoids 30s networkidle timeout on Cloudflare-protected sites
            html = await self.render(url, wait_until="load")
            if not html:
                break

            events = self._parse_articles(html)
            if not events:
                # Log a snippet to diagnose selector mismatches
                from bs4 import BeautifulSoup as _BS
                _soup = _BS(html, "html.parser")
                _body_classes = [a.get("class") for a in _soup.select("article")[:3]]
                logger.info(
                    f"[{self.name}] No articles on page {page_num} "
                    f"(article classes found: {_body_classes}), stopping"
                )
                break

            added = 0
            for ev in events:
                if ev.url not in seen_urls:
                    seen_urls.add(ev.url)
                    all_events.append(ev)
                    added += 1

            logger.info(f"[{self.name}] Page {page_num}: {added} events ({len(all_events)} total)")

            if added == 0:
                break

        return all_events
