import asyncio
import logging
import re

from database import upsert_event, export_csv, get_conn
from scrapers.bloomington_in.iu_events import IUEventsScraper
from scrapers.bloomington_in.parks import BloomingtonParksScraper
from scrapers.bloomington_in.eventbrite import EventbriteScraper
from scrapers.bloomington_in.bloomingtonian import BloomingtonianScraper
from scrapers.bloomington_in.visit_bloomington import VisitBloomingtonScraper

logger = logging.getLogger(__name__)


def city_to_slug(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')


CITIES: dict[str, dict] = {
    "bloomington-in": {
        "name": "Bloomington Area, IN",
        "scrapers": [
            IUEventsScraper(),
            BloomingtonParksScraper(),
            EventbriteScraper(),
            BloomingtonianScraper(),
            VisitBloomingtonScraper(),
        ],
    },
}

_iu_scraper_ref = CITIES["bloomington-in"]["scrapers"][0]


def _purge_internal_iu_events() -> None:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, description FROM events WHERE source = ?",
            ("IU Bloomington Calendar",),
        ).fetchall()
        to_delete = [
            row["id"] for row in rows
            if _iu_scraper_ref._is_internal(row["title"], row["description"])
        ]
        if to_delete:
            conn.execute(
                f"DELETE FROM events WHERE id IN ({','.join('?' * len(to_delete))})",
                to_delete,
            )
            conn.commit()
            logger.info(f"Purged {len(to_delete)} internal IU events from DB")


async def run_city_scrapers(city_slug: str) -> int:
    city_info = CITIES.get(city_slug)
    if not city_info:
        logger.error(f"Unknown city slug: {city_slug}")
        return 0

    city_name = city_info["name"]
    scrapers = city_info["scrapers"]
    logger.info(f"Scraping {city_name}...")

    results = await asyncio.gather(
        *[s.scrape() for s in scrapers],
        return_exceptions=True,
    )

    total = 0
    for scraper, result in zip(scrapers, results):
        if isinstance(result, Exception):
            logger.error(f"[{scraper.name}] Scraper crashed: {result}")
            continue
        for event in result:
            upsert_event(
                title=event.title,
                url=event.url,
                source=event.source,
                description=event.description,
                date=event.date,
                end_date=event.end_date,
                time=event.time,
                venue=event.venue,
                address=event.address,
                image_url=event.image_url,
                category=event.category,
                city=event.city or city_name,
            )
        total += len(result)
        logger.info(f"[{scraper.name}] Saved {len(result)} events")

    return total


async def run_all_scrapers() -> None:
    logger.info("Starting scrape run...")
    total = 0
    for city_slug in CITIES:
        total += await run_city_scrapers(city_slug)
    logger.info(f"Scrape run complete. Total events saved: {total}")
    _purge_internal_iu_events()
    export_csv()
