import asyncio
import logging
import re
from datetime import date as _date, timedelta

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from scrapers.base import BaseScraper, Event

logger = logging.getLogger(__name__)


class VisitBloomingtonScraper(BaseScraper):
    name = "Visit Bloomington"
    base_url = "https://www.visitbloomington.com/events/"

    MONTH_MAP = {
        "jan": "01", "feb": "02", "mar": "03", "apr": "04",
        "may": "05", "jun": "06", "jul": "07", "aug": "08",
        "sep": "09", "oct": "10", "nov": "11", "dec": "12",
    }

    def _parse_date(self, month_text: str, day_text: str) -> str | None:
        month_num = self.MONTH_MAP.get(month_text.strip().lower()[:3])
        if not month_num:
            return None
        try:
            day = day_text.strip().zfill(2)
            today = _date.today()
            year = today.year if int(month_num) >= today.month else today.year + 1
            return f"{year}-{month_num}-{day}"
        except Exception:
            return None

    def _parse_until_date(self, text: str) -> str | None:
        match = re.search(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", text)
        if not match:
            return None
        month_num = self.MONTH_MAP.get(match.group(1).lower()[:3])
        if not month_num:
            return None
        return f"{match.group(3)}-{month_num}-{match.group(2).zfill(2)}"

    def _expand_recurring(
        self, base_url: str, start_date: str | None, recurrence_text: str | None
    ) -> list[tuple[str | None, str]]:
        """Return (date_iso, unique_url) pairs for every upcoming occurrence."""
        fallback = (start_date, f"{base_url}#{start_date}" if start_date else base_url)
        if not recurrence_text or not start_date:
            return [fallback]

        text = recurrence_text.lower()
        if "daily" in text:
            step: timedelta | None = timedelta(days=1)
        elif "weekly" in text:
            step = timedelta(weeks=1)
        elif "monthly" in text:
            step = None  # handled below
        else:
            return [fallback]

        until_m = re.search(r"until\s+([\w\s,.]+)", text)
        end_date_str = self._parse_until_date(until_m.group(1)) if until_m else None
        if not end_date_str:
            return [fallback]

        try:
            current = _date.fromisoformat(start_date)
            end = _date.fromisoformat(end_date_str)
            today = _date.today()
        except ValueError:
            return [fallback]

        results = []
        while current <= end:
            if current >= today:
                ds = current.isoformat()
                results.append((ds, f"{base_url}#{ds}"))
            if step:
                current += step
            else:
                m = current.month + 1
                y = current.year + (1 if m > 12 else 0)
                m = 1 if m > 12 else m
                try:
                    current = _date(y, m, current.day)
                except ValueError:
                    current = _date(y, m, 28)

        return results if results else [fallback]

    def _infer_category(self, title: str) -> str:
        t = title.lower()
        if any(k in t for k in ("concert", "music", "band", "jazz", "orchestra", "choir", "recital", "symphony", "singer", "bluegrass", "folk", "indie", "acoustic", "open mic")):
            return "Music"
        if any(k in t for k in ("theater", "theatre", "dance", "ballet", "comedy", "film", "screening", "art", "gallery", "exhibit", "poetry", "storytelling", "improv", "puppet")):
            return "Arts & Performance"
        if any(k in t for k in ("food", "drink", "beer", "wine", "tasting", "dinner", "brunch", "culinary", "chef", "cocktail", "brew", "distill")):
            return "Food & Drink"
        if any(k in t for k in ("run", "race", "5k", "10k", "marathon", "hike", "bike", "cycle", "sport", "fitness", "yoga", "swim", "tournament", "golf", "tennis", "climb")):
            return "Sports & Recreation"
        if any(k in t for k in ("kids", "children", "youth", "family", "camp", "storytime", "teen", "junior")):
            return "Family & Youth"
        if any(k in t for k in ("lecture", "talk", "seminar", "workshop", "class", "training", "education", "symposium", "panel")):
            return "Lectures & Education"
        if any(k in t for k in ("spiritual", "faith", "prayer", "church", "religious", "ministry")):
            return "Religion & Spirituality"
        return "Community"

    def _parse_html(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        results = []

        for item in soup.select(".item[data-type='events']"):
            title_a = item.select_one("a.title")
            if not title_a:
                continue

            title = title_a.get_text(strip=True)
            href = title_a.get("href", "").strip()
            if not title or not href:
                continue

            base_url = f"https://www.visitbloomington.com{href}" if href.startswith("/") else href

            month_el = item.select_one(".mini-date-container .month")
            day_el = item.select_one(".mini-date-container .day")
            date_str = self._parse_date(
                month_el.get_text(strip=True) if month_el else "",
                day_el.get_text(strip=True) if day_el else "",
            )

            img = item.select_one("img.thumb")
            image_url = img.get("src") if img else None

            recur_li = item.select_one("li.recurrence")
            recurrence_text = recur_li.get_text(" ", strip=True) if recur_li else None

            results.append({
                "title": title,
                "base_url": base_url,
                "date": date_str,
                "image_url": image_url,
                "recurrence_text": recurrence_text,
            })

        return results

    def _try_next_page(self, page) -> bool:
        selectors = [
            "a.nxt",  # SimpleView standard
            "a[aria-label='Next page']",
            "a[aria-label='Go to next page']",
            "a[rel='next']",
            ".pagination a.next",
            ".pagination-next a",
            "li.next a",
            ".sv-pagination .next a",
            ".pager-next a",
            "button:has-text('Next')",
            "a:has-text('Next')",
        ]
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if el.count() > 0 and el.is_visible(timeout=800):
                    el.click()
                    page.wait_for_load_state("networkidle", timeout=10000)
                    return True
            except Exception:
                continue

        # Fallback: try scrolling to trigger infinite scroll
        try:
            prev_height = page.evaluate("document.body.scrollHeight")
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(2000)
            new_height = page.evaluate("document.body.scrollHeight")
            if new_height > prev_height:
                return True
        except Exception:
            pass

        return False

    def _scrape_sync(self) -> list[Event]:
        all_events: list[Event] = []
        seen_keys: set[str] = set()

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.set_extra_http_headers(self.HEADERS)
                page.goto(self.base_url, wait_until="networkidle", timeout=30000)

                try:
                    page.wait_for_selector(".item[data-type='events']", timeout=15000)
                except Exception:
                    logger.warning(f"[{self.name}] No events found on page load")
                    browser.close()
                    return []

                for page_num in range(1, 21):
                    raw = self._parse_html(page.content())
                    added = 0
                    for r in raw:
                        occurrences = self._expand_recurring(
                            r["base_url"], r["date"], r.get("recurrence_text")
                        )
                        for date_str, unique_url in occurrences:
                            if unique_url not in seen_keys:
                                seen_keys.add(unique_url)
                                all_events.append(Event(
                                    title=r["title"],
                                    url=unique_url,
                                    source=self.name,
                                    date=date_str,
                                    image_url=r["image_url"],
                                    category=self._infer_category(r["title"]),
                                    city=self.city,
                                ))
                                added += 1

                    logger.info(f"[{self.name}] Page {page_num}: {added} new events ({len(all_events)} total)")

                    prev_first_recid = page.locator(".item[data-type='events']").first.get_attribute("data-recid") if page.locator(".item[data-type='events']").count() > 0 else None

                    if not self._try_next_page(page):
                        logger.info(f"[{self.name}] No further pages after page {page_num}")
                        break

                    # Wait for content to actually change, not just network idle
                    try:
                        page.wait_for_function(
                            f"""() => {{
                                const first = document.querySelector('.item[data-type="events"]');
                                return first && first.getAttribute('data-recid') !== {repr(prev_first_recid)};
                            }}""",
                            timeout=8000,
                        )
                    except Exception:
                        # If content didn't change, we've hit the last page
                        logger.info(f"[{self.name}] Content unchanged after page {page_num}, stopping")
                        break

                browser.close()
        except Exception as e:
            logger.error(f"[{self.name}] Scrape failed: {e}")

        return all_events

    async def scrape(self) -> list[Event]:
        return await asyncio.to_thread(self._scrape_sync)
