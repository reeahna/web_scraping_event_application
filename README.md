# Web Scraping Event Application

This repository is being migrated to a new architecture. It currently contains two applications:

- [`legacy_app/`](legacy_app/) — the existing, working city events aggregator (FastAPI + APScheduler + SQLite + Playwright scrapers). Preserved unmodified; see [`legacy_app/README.md`](legacy_app/README.md) for setup and run instructions.
- [`new_app/`](new_app/) — placeholder for the replacement application. Currently empty.

See [`docs/migration-notes.md`](docs/migration-notes.md) for what changed during the repository reorganization and why.

## Quick start (legacy app)

```bash
cd legacy_app
pip install -r requirements.txt
playwright install chromium
uvicorn main:app --reload --port 8000
```

Then open http://localhost:8000
