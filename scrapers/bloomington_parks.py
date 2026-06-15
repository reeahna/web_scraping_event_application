"""
scrapers/bloomington_parks.py

Scrapes community events from the City of Bloomington Parks & Recreation page.
This is a static HTML page — no JavaScript rendering needed.
URL: https://bloomington.in.gov/parks/events
"""

import re
from bs4 import BeautifulSoup
from scrapers.base import BaseScraper, Event

try:
    from scrapers.base import BaseScraper, Event
except ImportError:  # pragma: no cover - supports flat project layout
    from base import BaseScraper, Event


class BloomingtonParksScraper(BaseScraper):
    name = "Bloomington Parks & Rec"
    base_url = "https://bloomington.in.gov/parks/events"

    MONTHS = {
        "january": "01", "february": "02", "march": "03", "april": "04",
        "may": "05", "june": "06", "july": "07", "august": "08",
        "september": "09", "october": "10", "november": "11", "december": "12",
        "jan": "01", "feb": "02", "mar": "03", "apr": "04",
        "jun": "06", "jul": "07", "aug": "08",
        "sep": "09", "oct": "10", "nov": "11", "dec": "12",
    }

    def _parse_date(self, month_heading: str, date_text: str) -> str | None:
        year_match = re.search(r"\b(\d{4})\b", month_heading)
        year = year_match.group(1) if year_match else "2026"

        date_match = re.search(
            r"\b(january|february|march|april|may|june|july|august|september|october|november|december|"
            r"jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\.?\s+(\d{1,2})\b",
            date_text.lower(),
        )
        if date_match:
            month_num = self.MONTHS.get(date_match.group(1), "01")
            day = date_match.group(2).zfill(2)
            return f"{year}-{month_num}-{day}"

        return None

    def _parse_venue(self, text: str) -> str | None:
        match = re.search(r"\bat\s+(.+)$", text, re.IGNORECASE)
        return match.group(1).strip() if match else None

    def _clean_title(self, text: str) -> str:
        parts = text.split("-", 1)
        return parts[0].strip()

    async def scrape(self) -> list[Event]:
        html = await self.fetch(self.base_url)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        events = []

        upcoming_header = soup.find(
            lambda tag: tag.name in ("h3", "h4", "h2") and
            "upcoming events" in tag.get_text(strip=True).lower()
        )
        if not upcoming_header:
            return events

        current_month = ""

        for sibling in upcoming_header.find_next_siblings():
            tag_name = sibling.name

            if tag_name in ("h3", "h4") and re.search(r"\b20\d{2}\b", sibling.get_text()):
                current_month = sibling.get_text(strip=True)
                continue

            if tag_name in ("h2", "h3") and "upcoming" not in sibling.get_text().lower():
                break

            if tag_name == "ul":
                for li in sibling.find_all("li"):
                    raw_text = li.get_text(" ", strip=True)
                    if not raw_text:
                        continue

                    if re.search(r"\b(second|third|every|weekly|monthly)\b", raw_text.lower()):
                        title = self._clean_title(raw_text)
                        venue = self._parse_venue(raw_text)
                        link_tag = li.find("a")
                        url = link_tag["href"] if link_tag else self.base_url
                        if url.startswith("/"):
                            url = "https://bloomington.in.gov" + url
                        events.append(Event(
                            title=title,
                            url=url,
                            source=self.name,
                            date=None,
                            venue=venue,
                            address="Bloomington, IN",
                            category="Community",
                        ))
                        continue

                    title = self._clean_title(raw_text)
                    date = self._parse_date(current_month, raw_text)
                    venue = self._parse_venue(raw_text)

                    link_tag = li.find("a")
                    url = link_tag["href"] if link_tag else self.base_url
                    if url.startswith("/"):
                        url = "https://bloomington.in.gov" + url

                    events.append(Event(
                        title=title,
                        url=url,
                        source=self.name,
                        date=date,
                        venue=venue,
                        address="Bloomington, IN",
                        category="Parks & Recreation",
                    ))

        return events
