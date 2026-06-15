import asyncio
import html
import logging
import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from scrapers.base import BaseScraper, Event

logger = logging.getLogger(__name__)


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


class BethlehemScraper(BaseScraper):
    name = "Discover Lehigh Valley - Bethlehem"
    base_url = "https://www.discoverlehighvalley.com/cities-towns/bethlehem/events/"
    root_url = "https://www.discoverlehighvalley.com"
    city = "Bethlehem, PA"

    MONTHS = {
        "jan": "01", "january": "01",
        "feb": "02", "february": "02",
        "mar": "03", "march": "03",
        "apr": "04", "april": "04",
        "may": "05",
        "jun": "06", "june": "06",
        "jul": "07", "july": "07",
        "aug": "08", "august": "08",
        "sep": "09", "sept": "09", "september": "09",
        "oct": "10", "october": "10",
        "nov": "11", "november": "11",
        "dec": "12", "december": "12",
    }

    def _clean(self, text: str | None) -> str:
        return " ".join(str(text or "").split()).strip()

    def _absolute_url(self, href: str | None) -> str | None:
        if not href:
            return None
        return urljoin(self.root_url, href)

    def _extract_category(self, card) -> str | None:
        tag = card.find(attrs={"data-dms-category-name": True})
        if not tag:
            return None
        raw = html.unescape(tag.get("data-dms-category-name", ""))
        raw = raw.replace("&amp;", "&")
        raw = self._clean(raw)
        return CATEGORY_MAP.get(raw.lower(), raw) or None

    def _extract_image_url(self, card) -> str | None:
        img = card.find("img", attrs={"data-srcset": True}) or card.find("img", src=True)
        srcset = img.get("data-srcset") if img else None

        if not srcset:
            bg = card.find(attrs={"data-bgset": True})
            srcset = bg.get("data-bgset") if bg else None

        if srcset:
            first = srcset.split(",")[0].strip().split(" ")[0]
            return html.unescape(first)

        if img and img.get("src") and "transparent.png" not in img.get("src", ""):
            return self._absolute_url(img.get("src"))

        return None

    def _parse_single_date(self, text: str) -> str | None:
        match = re.search(r"\b([A-Za-z]+)\.?\s+(\d{1,2})(?:,\s*(\d{4}))?\b", text)
        if not match:
            return None
        month = self.MONTHS.get(match.group(1).lower())
        if not month:
            return None
        day = match.group(2).zfill(2)
        year = match.group(3) or str(datetime.now().year)
        return f"{year}-{month}-{day}"

    def _parse_date_range(self, date_text: str) -> tuple[str | None, str | None]:
        text = self._clean(date_text)
        if not text:
            return None, None
        parts = text.split(" - ", 1)
        start = self._parse_single_date(parts[0])
        end = self._parse_single_date(parts[1]) if len(parts) > 1 else None
        return start, end

    def _extract_address_parts(self, card) -> tuple[str | None, str | None]:
        address_box = card.select_one(".card__address")
        if not address_box:
            return None, None
        parts = [
            self._clean(span.get_text(" ", strip=True))
            for span in address_box.find_all("span")
            if self._clean(span.get_text(" ", strip=True))
        ]
        if not parts:
            return None, None
        venue = parts[0]
        address = ", ".join(parts[1:]) if len(parts) > 1 else None
        return venue, address

    def _parse_card(self, card) -> Event | None:
        title_tag = card.select_one(".card__heading[href]")
        if not title_tag:
            title_tag = card.select_one("a[href*='/events/'][aria-label]")
        if not title_tag:
            return None

        title = self._clean(
            title_tag.get("data-dms-partner-name")
            or title_tag.get_text(" ", strip=True)
            or title_tag.get("aria-label")
        )
        url = self._absolute_url(title_tag.get("href"))
        if not title or not url:
            return None

        date_text = ""
        date_tag = card.select_one(".card__date-heading")
        if date_tag:
            date_text = date_tag.get_text(" ", strip=True)

        date, end_date = self._parse_date_range(date_text)
        venue, address = self._extract_address_parts(card)

        summary = None
        summary_tag = card.select_one(".map-infowindow__summary")
        if summary_tag:
            summary = self._clean(summary_tag.get_text(" ", strip=True))[:500] or None

        category = self._extract_category(card) or "Community"
        image_url = self._extract_image_url(card)

        return Event(
            title=title,
            url=url,
            source=self.name,
            description=summary,
            date=date,
            end_date=end_date,
            time=None,
            venue=venue,
            address=address,
            image_url=image_url,
            category=category,
            city=self.city,
        )

    def _scroll_to_load_cards(self, page) -> None:
        """Incrementally scroll until card count stabilises for 3 consecutive steps."""
        consecutive_stable = 0
        while consecutive_stable < 3:
            prev = page.locator(".card[data-entry-id]").count()
            page.evaluate("window.scrollBy(0, 600)")
            page.wait_for_timeout(700)
            if page.locator(".card[data-entry-id]").count() == prev:
                consecutive_stable += 1
            else:
                consecutive_stable = 0
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(800)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(400)

    def _scrape_sync(self) -> list[Event]:
        events: list[Event] = []
        seen_urls: set[str] = set()
        first_entry_id: str | None = None

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.set_extra_http_headers(self.HEADERS)

                for page_num in range(1, 50):
                    url = self.base_url if page_num == 1 else f"{self.base_url}?page={page_num}"
                    page.goto(url, wait_until="networkidle", timeout=30000)
                    self._scroll_to_load_cards(page)

                    # Detect if site looped back to page 1 (no real page N)
                    locator = page.locator(".card[data-entry-id]")
                    if locator.count() == 0:
                        logger.info(f"[{self.name}] No cards on page {page_num}, stopping")
                        break
                    current_first_id = locator.first.get_attribute("data-entry-id")
                    if page_num == 1:
                        first_entry_id = current_first_id
                    elif current_first_id == first_entry_id:
                        logger.info(f"[{self.name}] Page {page_num} same as page 1, stopping")
                        break

                    soup = BeautifulSoup(page.content(), "html.parser")
                    added = 0
                    for card in soup.select(".card[data-entry-id]"):
                        event = self._parse_card(card)
                        if not event or event.url in seen_urls:
                            continue
                        seen_urls.add(event.url)
                        events.append(event)
                        added += 1

                    logger.info(f"[{self.name}] Page {page_num}: {added} new events ({len(events)} total)")

                    if added == 0 and page_num > 1:
                        logger.info(f"[{self.name}] No new events on page {page_num}, stopping")
                        break

                browser.close()
        except Exception as e:
            logger.error(f"[{self.name}] Playwright failed: {e}")

        return events

    async def scrape(self) -> list[Event]:
        return await asyncio.to_thread(self._scrape_sync)
