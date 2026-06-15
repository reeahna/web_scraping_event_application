# City Events Aggregator

A Python web app that scrapes event listings from multiple city websites and displays them in one mobile-friendly feed. Scrapes automatically every 5 minutes.

## Stack

| Layer | Tech |
|-------|------|
| Backend | FastAPI + APScheduler |
| Scraping | httpx + BeautifulSoup (+ Playwright for JS sites) |
| Storage | SQLite |
| Frontend | Jinja2 + vanilla JS |

## Setup

```bash
# 1. Create a virtual environment
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the app
uvicorn main:app --reload --port 8000
```

Then open http://localhost:8000

## Adding a New Site

1. Copy `scrapers/eventbrite.py` to `scrapers/my_site.py`
2. Set `name` and `base_url`
3. Implement `scrape()` — use BeautifulSoup to parse the HTML and return a list of `Event` objects
4. Register it in `scraper_runner.py`:

```python
from scrapers.my_site import MySiteScraper

SCRAPERS = [
    EventbriteScraper(),
    MySiteScraper(),   # <-- add here
]
```

## Adding a JS-Rendered Site

Some sites load events via JavaScript (React, etc.) and won't work with plain httpx.
For those, use Playwright:

```bash
pip install playwright
playwright install chromium
```

Then in your scraper, replace `self.fetch()` with:

```python
from playwright.async_api import async_playwright

async def scrape(self):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto(self.base_url)
        await page.wait_for_selector("YOUR_SELECTOR")
        html = await page.content()
        await browser.close()

    soup = BeautifulSoup(html, "html.parser")
    # ... rest of parsing
```

## Project Structure

```
cityevents/
  main.py              # FastAPI app + scheduler
  database.py          # SQLite setup and queries
  scraper_runner.py    # Runs all scrapers, saves to DB
  scrapers/
    base.py            # BaseScraper class + Event dataclass
    eventbrite.py      # Example scraper (copy to add new ones)
  templates/
    index.html         # Mobile-friendly events feed
  requirements.txt
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_PATH` | `events.db` | Path to the SQLite database file |
