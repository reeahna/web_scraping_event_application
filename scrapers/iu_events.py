import html as html_lib
import logging
import re
from datetime import date as _date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from scrapers.base import BaseScraper, Event

logger = logging.getLogger(__name__)

_INTERNAL_RE = re.compile(
    r"^(block\b|blocked\b|hold\b|holds?\b|maintenance|renovation|storage|staging|uits[-\s]|iuh-|slhs-|iuc\b|closed for|temp offices?|chair management|bridge week|orientation storage|complex processes|holiday-no|year \d|summer \d{4}:|withdrawal|pass/fail)",
    re.IGNORECASE,
)
_INTERNAL_CONTAINS_RE = re.compile(
    r"(lab\s+closed|a/v\s+installation|no\s+conference|\bclosed\s*[-–]\s*summer|\broom\s+closed\b|temp\s+office"
    r"|-storage-|\(homebase\)|\(meeting room\)|\(work\s*space\)|\(conference room\)|\(classroom\)|\(atrium\)|\(workroom\))",
    re.IGNORECASE,
)
_COURSE_CODE_RE = re.compile(r"^[A-Z]+-[A-Z] \d{3} \d{4}")
_INTERNAL_DESC_RE = re.compile(
    r"(event locator:|requestor email|expected headcount:|requestor'?s? name:|are you filling this out for|select your primary role:)",
    re.IGNORECASE,
)


class IUEventsScraper(BaseScraper):
    name = "IU Bloomington Calendar"
    api_url = "https://events.iu.edu/live/json/events"
    optional_fields = "location,tags,image,summary,event_types,group_title"

    def _clean(self, text: str) -> str:
        return " ".join(html_lib.unescape(str(text)).split()).strip()

    def _is_internal(self, title: str, description: str | None = None) -> bool:
        if _INTERNAL_RE.match(title) or _COURSE_CODE_RE.match(title) or _INTERNAL_CONTAINS_RE.search(title):
            return True
        if description and _INTERNAL_DESC_RE.search(description):
            return True
        return False

    def _infer_category(self, text: str) -> str | None:
        if any(k in text for k in ("exhibition", "exhibit", "museum", "gallery", "collection")):
            return "Arts & Museum Exhibitions"
        if any(k in text for k in ("concert", "music", "recital", "orchestra", "jazz", "choir", "opera", "band", "symphony", "guitar", "violin", "piano")):
            return "Music"
        if any(k in text for k in ("theater", "theatre", "dance", "ballet", "comedy", "film", "screening", "storytelling", "dramatic")):
            return "Arts & Performance"
        if any(k in text for k in ("lecture", "talk", "seminar", "colloquium", "symposium", "roundtable", "panel", "summit", "forum", "info session")):
            return "Lectures & Education"
        if any(k in text for k in ("conference", "convention", "congress")):
            return "Conferences"
        if any(k in text for k in ("workshop", "training", "course", "tutoring", "learning lab", "office hours", "advising")):
            return "Workshops & Training"
        if any(k in text for k in ("sport", "race", "run", "swim", "match", "tournament", "athlete", "football", "basketball", "soccer", "track", "tennis", "volleyball", "golf", "wrestling", "crew")):
            return "Sports & Recreation"
        if any(k in text for k in ("food", "drink", "dinner", "lunch", "brunch", "tasting", "reception", "banquet", "coffee chat", "cookout", "barbecue")):
            return "Food & Drink"
        if any(k in text for k in ("career", "networking", "business", "entrepreneur", "hiring", "job", "internship", "mba", "management", "economic", "nonprofit", "philanthropy")):
            return "Business & Career"
        if any(k in text for k in ("health", "wellness", "yoga", "pilates", "fitness", "meditation", "mental health", "therapeutics", "medicine", "clinical", "cancer", "ortho")):
            return "Health & Wellness"
        if any(k in text for k in ("camp", "kids", "youth", "children", "family", "teen", "junior", "summer academy")):
            return "Family & Youth"
        if any(k in text for k in ("ai ", "artificial intelligence", "data science", "software", "coding", "programming", "game development", "technology", "cybersecurity")):
            return "Technology"
        if any(k in text for k in ("research", "dissertation", "thesis", "phd", "graduate", "grad school", "postdoc", "academic")):
            return "Research & Academic"
        if any(k in text for k in ("alumni", "reunion", "homecoming", "pride", "community", "volunteer", "fundraiser", "festival", "fair", "market")):
            return "Community"
        if any(k in text for k in ("spiritual", "religious", "faith", "prayer", "church", "ministry", "interfaith")):
            return "Religion & Spirituality"
        return None

    def _parse_event(self, raw: dict) -> Event | None:
        title = self._clean(raw.get("title") or "")
        url = (raw.get("url") or "").strip()
        if not title or not url:
            return None

        summary = raw.get("summary") or ""
        description_raw = self._clean(summary)[:500] or None

        if raw.get("is_canceled") or self._is_internal(title, description_raw):
            return None

        date = None
        end_date = None
        time = None
        tz = ZoneInfo(raw.get("timezone") or "America/Indiana/Indianapolis")
        date_iso = raw.get("date_iso")
        if date_iso:
            try:
                dt = datetime.fromisoformat(date_iso).astimezone(tz)
                date = dt.date().isoformat()
                if not raw.get("is_all_day") and raw.get("date_time"):
                    time = raw["date_time"].split(" - ")[0].strip()
            except Exception:
                pass
        # For repeating events use repeats_end; otherwise fall back to date2_iso
        if raw.get("repeats"):
            for field in ("repeats_end", "repeats_until", "date2_iso"):
                val = raw.get(field)
                if val:
                    try:
                        end_date = datetime.fromisoformat(str(val)).astimezone(tz).date().isoformat()
                        break
                    except Exception:
                        if isinstance(val, str) and len(val) >= 10:
                            end_date = val[:10]
                            break
        else:
            end_date_iso = raw.get("date2_iso")
            if end_date_iso:
                try:
                    end_date = datetime.fromisoformat(end_date_iso).astimezone(tz).date().isoformat()
                except Exception:
                    pass

        location = raw.get("location") or {}
        venue = self._clean(location.get("room") or location.get("building") or "") or None
        address = self._clean(location.get("address") or "") or None

        image = raw.get("image") or {}
        image_url = image.get("url") or image.get("src") or None

        description = description_raw
        text = (title + " " + (description or "")).lower()
        category = self._infer_category(text)

        return Event(
            title=title,
            url=url,
            source=self.name,
            description=description,
            date=date,
            end_date=end_date,
            time=time,
            venue=venue,
            address=address,
            image_url=image_url,
            category=category or "IU Bloomington",
        )

    async def scrape(self) -> list[Event]:
        events: list[Event] = []
        seen_urls: set[str] = set()
        seen_gids: set[str] = set()

        async with httpx.AsyncClient(
            headers=self.HEADERS,
            follow_redirects=True,
            timeout=25,
        ) as client:
            page = 1
            while True:
                try:
                    r = await client.get(
                        self.api_url,
                        params={
                            "page": page,
                            "fields": self.optional_fields,
                            "from": (_date.today() - timedelta(days=90)).isoformat(),
                        },
                    )
                    r.raise_for_status()
                    data = r.json()
                except Exception as e:
                    logger.error(f"[{self.name}] API request failed page {page}: {e}")
                    break

                meta = data.get("meta", {})
                raw_events = data.get("data", [])

                if not raw_events:
                    break

                for raw in raw_events:
                    gid = str(raw.get("gid") or "")
                    if raw.get("repeats") and gid and gid in seen_gids:
                        continue
                    event = self._parse_event(raw)
                    if not event or event.url in seen_urls:
                        continue
                    seen_urls.add(event.url)
                    if raw.get("repeats") and gid:
                        seen_gids.add(gid)
                    events.append(event)

                if page >= meta.get("total_pages", 1):
                    break

                page += 1

        return events
