# City Events Aggregator (Legacy App)

A Python web app that scrapes event listings from multiple city websites and displays them in one mobile-friendly feed. Scrapes automatically every 5 minutes.

This is the **legacy application**, preserved as-is under `legacy_app/` as part of the Phase 0 repository reorganization. See [`../docs/migration-notes.md`](../docs/migration-notes.md) for what changed during relocation.

## Stack

| Layer | Tech |
|-------|------|
| Backend | FastAPI + APScheduler |
| Scraping | httpx + BeautifulSoup (+ Playwright for JS sites) |
| Storage | SQLite |
| Frontend | Jinja2 + vanilla JS |

## Setup

```bash
# 1. From the repo root, create a virtual environment (or reuse the existing root venv/)
python -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate

# 2. Install dependencies
pip install -r legacy_app/requirements.txt

# 3. Install Playwright's browser binaries (one-time, user-level cache — not repo-relative)
playwright install chromium

# 4. Run the app — IMPORTANT: the working directory must be legacy_app/,
#    because database.py, main.py's template/static mounts, and the CSV
#    export all use paths relative to the current working directory.
cd legacy_app
uvicorn main:app --reload --port 8000
```

Then open http://localhost:8000

## Adding a New Site

1. Copy `scrapers/bloomington_in/eventbrite.py` to `scrapers/<city>/my_site.py`
2. Set `name` and `base_url`
3. Implement `scrape()` — use BeautifulSoup to parse the HTML and return a list of `Event` objects
4. Register it in `scraper_runner.py` under the relevant city's `"scrapers"` list.

## Adding a JS-Rendered Site

Some sites load events via JavaScript (React, etc.) and won't work with plain httpx. For those, use `self.render()` from `BaseScraper` (Playwright-backed), or see `scrapers/base.py` for the underlying implementation.

## Project Structure

```
legacy_app/
  main.py              # FastAPI app + scheduler
  database.py          # SQLite setup and queries
  scraper_runner.py    # Runs all scrapers per city, saves to DB
  geocoder.py          # Nominatim geocoding for event lat/lng
  scrapers/
    base.py            # BaseScraper class + Event dataclass
    bloomington_in/     # Bloomington, IN scrapers
    bethlehem_pa/        # Bethlehem, PA scrapers
  templates/
    home.html           # City picker landing page
    index.html          # Mobile-friendly events feed
  static/               # Currently unused (fonts/icons are CDN/inline)
  requirements.txt
  events.db             # SQLite database (gitignored)
  events.csv            # CSV export (gitignored)
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|--------------|
| `DB_PATH` | `events.db` | Path to the SQLite database file, resolved relative to the working directory the app is launched from (i.e. `legacy_app/`) |

## Known relocation-related notes

- All internal imports (`from database import ...`, `from scrapers.x import ...`) and relative paths (`events.db`, `events.csv`, `templates/`, `static/`) are **unchanged from the original code**. They resolve correctly only when the app is launched with `legacy_app/` as the working directory (see Setup above).
- No code was refactored, no modules renamed, no schemas or scraper logic changed during relocation.
