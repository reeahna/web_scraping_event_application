import asyncio
import logging

from database import upsert_event
from scrapers.visit_bloomington import VisitBloomingtonScraper
from scrapers.bloomington_parks import BloomingtonParksScraper

logger = logging.getLogger(__name__)

SCRAPERS = [
    VisitBloomingtonScraper(),
    BloomingtonParksScraper(),
]


async def run_all_scrapers():
    logger.info("Starting scrape run...")
    results = await asyncio.gather(
        *[scraper.scrape() for scraper in SCRAPERS],
        return_exceptions=True,
    )

    total = 0
    for scraper, result in zip(SCRAPERS, results):
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
                time=event.time,
                venue=event.venue,
                address=event.address,
                image_url=event.image_url,
                category=event.category,
            )
        total += len(result)
        logger.info(f"[{scraper.name}] Saved {len(result)} events")

    logger.info(f"Scrape run complete. Total events saved: {total}")