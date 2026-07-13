import json
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from scrapers.base import BaseScraper, Event


CATEGORY_SLUG_MAP = {
    "food-and-drink": "Food & Drink",
    "music": "Music",
    "arts": "Arts & Performance",
    "performing-visual-arts": "Arts & Performance",
    "film-media-entertainment": "Arts & Performance",
    "business": "Business & Career",
    "health": "Health & Wellness",
    "sports-fitness": "Sports & Recreation",
    "science-tech": "Technology",
    "community": "Community",
    "charity-causes": "Community",
    "education": "Lectures & Education",
    "family-education": "Family & Youth",
    "school-activities": "Workshops & Training",
    "hobbies": "Community",
    "travel-outdoor": "Sports & Recreation",
    "seasonal-holiday": "Community",
    "religion-spirituality": "Religion & Spirituality",
    "home-lifestyle": "Community",
    "fashion": "Arts & Performance",
    "government": "Business & Career",
}


class EventbriteScraper(BaseScraper):
    name = "Eventbrite"
    base_url = "https://www.eventbrite.com/d/in--bloomington/all-events/"

    # Towns in/adjacent to Monroe County, IN that count as "Bloomington area"
    _ALLOWED_LOCALITIES = {"Bloomington", "Ellettsville", "Stinesville", "Unionville", "Stanford"}

    def __init__(
        self,
        base_url: str | None = None,
        city: str | None = None,
        allowed_localities: set[str] | None = None,
    ):
        if base_url is not None:
            self.base_url = base_url
        if city is not None:
            self.city = city
        if allowed_localities is not None:
            self._ALLOWED_LOCALITIES = allowed_localities

    def _is_local_location(self, location: dict) -> bool:
        """Return False if the event has an address outside the Bloomington, IN area."""
        if not location:
            return True
        address_obj = location.get("address")
        if not isinstance(address_obj, dict):
            return True
        locality = (address_obj.get("addressLocality") or "").strip()
        region = (address_obj.get("addressRegion") or "").strip()
        if not locality:
            return True
        return locality in self._ALLOWED_LOCALITIES and region in {"IN", "Indiana"}

    def _clean(self, text: str) -> str:
        return " ".join(str(text).split()).strip()

    def _normalize_url(self, href: str) -> str:
        url = urljoin("https://www.eventbrite.com", href)
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    def _extract_event_links_from_listing(self, html: str) -> list[tuple[str, str, str | None]]:
        soup = BeautifulSoup(html, "html.parser")
        links: list[tuple[str, str, str | None]] = []
        seen: set[str] = set()

        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/e/" not in href:
                continue

            url = self._normalize_url(href)
            if url in seen:
                continue

            title = self._clean(a.get_text(" ", strip=True))
            if not title:
                card = a.find_parent(["section", "article", "div"]) or a
                title = self._clean(card.get_text(" ", strip=True))[:120]

            if not title:
                title = "Eventbrite Event"

            slug = a.get("data-event-category") or ""
            listing_category = CATEGORY_SLUG_MAP.get(slug)

            seen.add(url)
            links.append((url, title, listing_category))

        return links

    def _parse_json_ld_event(self, soup: BeautifulSoup) -> dict | None:
        for script in soup.find_all("script", type="application/ld+json"):
            raw = script.string or script.get_text() or ""
            if not raw.strip():
                continue

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            stack = data if isinstance(data, list) else [data]

            while stack:
                item = stack.pop(0)

                if isinstance(item, list):
                    stack.extend(item)
                    continue

                if not isinstance(item, dict):
                    continue

                if item.get("@type") == "Event":
                    return item

                graph = item.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)

        return None

    def _extract_address(self, location) -> tuple[str | None, str | None]:
        venue = None
        address = None

        if isinstance(location, dict):
            venue = self._clean(location.get("name") or "") or None
            address_obj = location.get("address")

            if isinstance(address_obj, dict):
                parts = [
                    address_obj.get("streetAddress"),
                    address_obj.get("addressLocality"),
                    address_obj.get("addressRegion"),
                    address_obj.get("postalCode"),
                ]
                address = ", ".join(self._clean(p) for p in parts if p) or None
            elif address_obj:
                address = self._clean(address_obj)

        return venue, address

    def _extract_image_url(self, item: dict, soup: BeautifulSoup) -> str | None:
        image_url = item.get("image")

        if isinstance(image_url, list):
            image_url = image_url[0] if image_url else None

        if isinstance(image_url, dict):
            image_url = image_url.get("url")

        if image_url:
            return str(image_url)

        og_image = soup.find("meta", property="og:image")
        if og_image and og_image.get("content"):
            return og_image["content"]

        return None

    def _extract_category(self, item: dict, soup: BeautifulSoup) -> str | None:
        for key in ("category", "genre", "keywords"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return self._clean(value.split(",")[0])
            if isinstance(value, list) and value:
                return self._clean(value[0])

        meta_candidates = [
            ("meta", {"property": "eventbrite:category"}),
            ("meta", {"name": "eventbrite:category"}),
            ("meta", {"property": "og:type"}),
        ]

        for tag_name, attrs in meta_candidates:
            tag = soup.find(tag_name, attrs=attrs)
            if tag and tag.get("content"):
                value = self._clean(tag["content"])
                if value and value.lower() not in {"website", "event", "events.event"}:
                    return value

        page_text = self._clean(soup.get_text(" ", strip=True)).lower()

        category_keywords = {
            "Music": ["concert", "music", "dj", "band", "live music", "singer", "orchestra", "jazz", "choir"],
            "Arts & Performance": ["art", "gallery", "theater", "theatre", "dance", "film", "painting", "poetry", "comedy", "screening", "exhibit", "exhibition"],
            "Food & Drink": ["food", "drink", "beer", "wine", "brunch", "dinner", "cocktail", "tasting", "culinary"],
            "Sports & Recreation": ["sports", "run", "race", "fitness", "game", "tournament", "athletic", "soccer", "basketball", "golf"],
            "Health & Wellness": ["yoga", "wellness", "health", "meditation", "pilates", "mindfulness", "healing"],
            "Business & Career": ["business", "networking", "entrepreneur", "startup", "career", "professional", "leadership", "marketing"],
            "Technology": ["tech", "software", "coding", "programming", "ai", "data", "cyber", "developer"],
            "Lectures & Education": ["lecture", "seminar", "talk", "panel", "symposium", "forum", "conference"],
            "Workshops & Training": ["workshop", "class", "training", "course", "certification", "bootcamp"],
            "Family & Youth": ["kids", "family", "children", "youth", "camp", "teen"],
            "Community": ["community", "fundraiser", "festival", "market", "volunteer", "charity", "social"],
            "Religion & Spirituality": ["spiritual", "religious", "faith", "prayer", "church", "ministry"],
        }

        for category, keywords in category_keywords.items():
            if any(keyword in page_text for keyword in keywords):
                return category

        return None

    def _detect_multiple_dates(self, soup: BeautifulSoup) -> str | None:
        page_text = soup.get_text(" ", strip=True).lower()
        if "multiple dates" in page_text or "select a date" in page_text or "choose a date" in page_text:
            return "Multiple Dates"
        return None

    def _extract_event_from_detail(self, html: str, url: str, fallback_title: str, listing_category: str | None = None) -> Event | None:
        soup = BeautifulSoup(html, "html.parser")
        item = self._parse_json_ld_event(soup)

        if item:
            title = self._clean(item.get("name") or fallback_title)
            if not title:
                return None

            start_date = str(item.get("startDate") or "")
            date = start_date[:10] if len(start_date) >= 10 else None
            time = start_date[11:16] if len(start_date) >= 16 else None
            if not date:
                date = self._detect_multiple_dates(soup)

            location = item.get("location") or {}
            if not self._is_local_location(location):
                return None
            venue, address = self._extract_address(location)
            image_url = self._extract_image_url(item, soup)

            description = item.get("description")
            if description:
                desc_str = str(description)
                if "<" in desc_str:
                    desc_str = BeautifulSoup(desc_str, "html.parser").get_text(" ", strip=True)
                description = self._clean(desc_str)

            category = self._extract_category(item, soup) or listing_category

            return Event(
                title=title,
                url=url,
                source=self.name,
                description=description[:500] if description else None,
                date=date,
                time=time,
                venue=venue,
                address=address,
                image_url=image_url,
                category=category,
                city=self.city,
            )

        og_title = soup.find("meta", property="og:title")
        title = self._clean(og_title.get("content", "")) if og_title else fallback_title
        if not title:
            return None

        og_description = soup.find("meta", property="og:description")
        description = (
            self._clean(og_description.get("content", ""))[:500]
            if og_description and og_description.get("content")
            else None
        )

        og_image = soup.find("meta", property="og:image")
        image_url = og_image.get("content") if og_image and og_image.get("content") else None

        return Event(
            title=title,
            url=url,
            source=self.name,
            description=description,
            image_url=image_url,
            date=self._detect_multiple_dates(soup),
            category=self._extract_category({}, soup) or listing_category,
            city=self.city,
        )

    async def scrape(self) -> list[Event]:
        events: list[Event] = []
        seen_urls: set[str] = set()

        for page_num in range(1, 8):
            page_url = self.base_url if page_num == 1 else f"{self.base_url}?page={page_num}"

            html = await self.render(page_url)
            if not html:
                break

            event_links = self._extract_event_links_from_listing(html)
            if not event_links:
                break

            page_added = 0

            for url, fallback_title, listing_category in event_links:
                if url in seen_urls:
                    continue

                detail_html = await self.fetch(url)
                if not detail_html:
                    continue

                event = self._extract_event_from_detail(detail_html, url, fallback_title, listing_category)
                if not event:
                    continue

                seen_urls.add(url)
                events.append(event)
                page_added += 1

            if page_added == 0 and page_num > 1:
                break

        return events
