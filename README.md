# Web Scraping Event Application

A city-events aggregator: a FastAPI application that discovers, extracts, and
schedules imports of public event listings, with a public site and an admin UI.

- [`new_app/`](new_app/) is the application — extraction patterns, the
  onboarding/inference pipeline, the restricted headless-browser fallback, the
  durable scheduler, authentication/RBAC, and the public + admin interfaces.

See [`new_app/README.md`](new_app/README.md) for local setup and verification,
and [`DEPLOY_RENDER.md`](DEPLOY_RENDER.md) for deploying to Render.

> The original standalone scraper (`legacy_app/`) has been removed now that
> `new_app/` fully replaces it; it remains in the git history if ever needed.
