import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from scrapers.base import BaseScraper, Event


class BloomingtonianScraper(BaseScraper):
    name = "The Bloomingtonian"
    base_url = "https://bloomingtonian.com/category/events-calendar/"

    MONTHS = {
        "january": "01", "february": "02", "march": "03", "april": "04",
        "may": "05", "june": "06", "july": "07", "august": "08",
        "september": "09", "october": "10", "november": "11", "december": "12",
        "jan": "01", "feb": "02", "mar": "03", "apr": "04",
        "jun": "06", "jul": "07", "aug": "08",
        "sep": "09", "sept": "09", "oct": "10", "nov": "11", "dec": "12",
    }

    DATE_RE = re.compile(
        r"\b("
        r"january|february|march|april|may|june|july|august|september|october|november|december|"
        r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
        r")\.?\s+(\d{1,2})(?:,\s*(\d{4}))?\b",
        re.IGNORECASE,
    )

    def _clean_text(self, text: str) -> str:
        return " ".join(text.split()).strip()

    def _page_url(self, page_num: int) -> str:
        if page_num == 1:
            return self.base_url
        return f"{self.base_url}page/{page_num}/"

    def _extract_image_url(self, container) -> str | None:
        a_tag = container.select_one("a.post-img")
        if a_tag:
            style = a_tag.get("style", "")
            match = re.search(r"url\(['\"]?(.*?)['\"]?\)", style)
            if match:
                return match.group(1)

        img_tag = container.find("img")
        if img_tag:
            return img_tag.get("src") or img_tag.get("data-src")

        return None

    def _published_date(self, card) -> str | None:
        date_link = card.select_one(".entry-meta .date a")
        if not date_link:
            return None

        text = self._clean_text(date_link.get_text(" ", strip=True))
        m = re.search(r"\b([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})\b", text)
        if not m:
            return None

        month_name, day, year = m.groups()
        month_map = {
            "January": "01", "February": "02", "March": "03", "April": "04",
            "May": "05", "June": "06", "July": "07", "August": "08",
            "September": "09", "October": "10", "November": "11", "December": "12",
        }
        month_num = month_map.get(month_name)
        if not month_num:
            return None
        return f"{year}-{month_num}-{day.zfill(2)}"

    def _strip_dateline_prefix(self, text: str) -> str:
        text = self._clean_text(text)

        # Common intro boilerplate on this site
        text = re.sub(r"^Written from [^.]+\.?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"^BLOOMINGTON,\s*Ind\.\s*—\s*", "", text, flags=re.IGNORECASE)

        return self._clean_text(text)

    def _derive_event_title(self, chunk: str, article_title: str) -> str:
        """
        Try to turn a sentence like:
          'Greg Mendez with Scarlet Rae at 9 p.m. June 15'
        into:
          'Greg Mendez with Scarlet Rae'
        """
        text = self._clean_text(chunk)

        # Remove the date itself and anything after it.
        m = self.DATE_RE.search(text)
        if m:
            text = text[:m.start()].strip()

        # Remove trailing time phrases.
        text = re.sub(
            r"\b(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:a\.m\.|p\.m\.)\.?$",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()

        # Remove common lead-in verbs / announcement phrasing.
        text = re.split(
            r"\b(?:will join|will perform|are scheduled to perform|is scheduled to perform|"
            r"are scheduled|is scheduled|will be|to perform|announced that|announces|announced)\b",
            text,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()

        # Clean off punctuation and stray separators.
        text = text.strip(" ,;:-—")

        if len(text) < 3:
            return article_title

        return text

    def _extract_event_dates(self, detail_html: str | None, published_date: str | None) -> list[tuple[str, str]]:
        """
        Return a list of (date, title_fragment) pairs from the article body.
        Each match becomes its own event.
        """
        if not detail_html:
            return []

        soup = BeautifulSoup(detail_html, "html.parser")
        content = soup.select_one(".entry-content") or soup.select_one("article") or soup

        published_year = published_date[:4] if published_date else None
        results: list[tuple[str, str]] = []
        seen_dates: set[tuple[str, str]] = set()

        for tag in content.find_all(["p", "li"]):
            raw = self._clean_text(tag.get_text(" ", strip=True))
            if not raw:
                continue

            raw = self._strip_dateline_prefix(raw)

            # Split long paragraphs on semicolons because the site often packs
            # multiple event announcements into one paragraph.
            chunks = [c.strip() for c in re.split(r"[;\n]+", raw) if c.strip()]

            for chunk in chunks:
                for match in self.DATE_RE.finditer(chunk):
                    month_name, day, year = match.groups()
                    month_num = self.MONTHS.get(month_name.lower())
                    if not month_num:
                        continue

                    year = year or published_year
                    if not year:
                        continue

                    event_date = f"{year}-{month_num}-{day.zfill(2)}"
                    title_fragment = self._derive_event_title(chunk, article_title="")
                    key = (event_date, title_fragment)
                    if key in seen_dates:
                        continue
                    seen_dates.add(key)
                    results.append((event_date, title_fragment))

        return results

    async def scrape(self) -> list[Event]:
        events: list[Event] = []
        seen_urls: set[str] = set()

        for page_num in range(1, 10):
            html = await self.fetch(self._page_url(page_num))
            if not html:
                break

            soup = BeautifulSoup(html, "html.parser")

            cards = soup.select("div.post-wrap > div.post-col > div.hentry")
            if not cards:
                cards = soup.select("div.hentry")

            if not cards:
                break

            page_added = 0

            for card in cards:
                title_link = card.select_one("h2.entry-title a, h3.entry-title a, a[rel='bookmark']")
                if not title_link:
                    continue

                article_title = self._clean_text(title_link.get_text(" ", strip=True))
                href = title_link.get("href", "").strip()
                if not article_title or not href:
                    continue

                article_url = urljoin("https://bloomingtonian.com", href)
                if article_url in seen_urls:
                    continue
                seen_urls.add(article_url)

                published_date = self._published_date(card)
                description_tag = card.select_one(".entry-content p, .entry-summary p, .entry-content, .entry-summary")
                description = self._clean_text(description_tag.get_text(" ", strip=True)) if description_tag else None
                image_url = self._extract_image_url(card)

                detail_html = await self.fetch(article_url)
                extracted_dates = self._extract_event_dates(detail_html, published_date)

                if extracted_dates:
                    for idx, (event_date, title_fragment) in enumerate(extracted_dates, start=1):
                        # Synthetic unique URL so each extracted event can be stored separately.
                        synthetic_url = f"{article_url}#event-{idx}"

                        title = title_fragment if title_fragment else article_title
                        if not title:
                            title = article_title

                        events.append(
                            Event(
                                title=title,
                                url=synthetic_url,
                                source=self.name,
                                description=description[:500] if description else None,
                                date=event_date,
                                image_url=image_url,
                                category="Local Events",
                            )
                        )
                        page_added += 1
                else:
                    # Fallback: keep the post as one item if no event date was found.
                    events.append(
                        Event(
                            title=article_title,
                            url=article_url,
                            source=self.name,
                            description=description[:500] if description else None,
                            date=published_date,
                            image_url=image_url,
                            category="Local Events",
                        )
                    )
                    page_added += 1

            if page_added == 0:
                break

        return events