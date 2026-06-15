from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional
import asyncio
import logging

import httpx
from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)


@dataclass
class Event:
    title: str
    url: str
    source: str
    description: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None
    venue: Optional[str] = None
    address: Optional[str] = None
    image_url: Optional[str] = None
    category: Optional[str] = None
    end_date: Optional[str] = None
    city: Optional[str] = None


class BaseScraper(ABC):
    name: str = "unnamed"
    base_url: str = ""
    city: str = "Bloomington Area, IN"

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36"
        )
    }

    async def fetch(self, url: str, **kwargs) -> Optional[str]:
        try:
            async with httpx.AsyncClient(
                headers=self.HEADERS,
                follow_redirects=True,
                timeout=20,
            ) as client:
                response = await client.get(url, **kwargs)
                response.raise_for_status()
                return response.text
        except Exception as e:
            logger.error(f"[{self.name}] Failed to fetch {url}: {e}")
            return None

    def _render_sync(self, url: str, wait_for_selector: str | None = None) -> Optional[str]:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                page.goto(url, wait_until="networkidle", timeout=30000)
                if wait_for_selector:
                    page.wait_for_selector(wait_for_selector, timeout=15000)
                html = page.content()
                browser.close()
                return html
        except Exception as e:
            logger.error(f"[{self.name}] Playwright render failed for {url}: {e}")
            return None

    async def render(self, url: str, wait_for_selector: str | None = None) -> Optional[str]:
        return await asyncio.to_thread(self._render_sync, url, wait_for_selector)

    @abstractmethod
    async def scrape(self) -> list[Event]:
        ...