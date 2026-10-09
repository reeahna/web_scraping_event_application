# Backend guide

FastAPI app in `app/`, run from this folder. Read the root `CLAUDE.md` first.

## Where things live

| Path | Role |
|---|---|
| `app/main.py` | App setup, middleware, router registration, `/static` mount |
| `app/config.py` | All settings (`get_settings()`), read from env vars / `.env` |
| `app/paths.py` | Location of `frontend/templates` and `frontend/static` |
| `app/routers/` | HTTP endpoints. Keep them thin: parse input, check permissions, call a service, `render()` a template or redirect |
| `app/services/` | Business logic and write paths |
| `app/repositories/` | Database queries |
| `app/models/` | SQLAlchemy models; schema changes need a migration in `migrations/versions/` |
| `app/schemas/` | Pydantic models for validated input and site configuration |
| `app/extraction/` | Scraping engine: fetch, detect, extract, normalize, validate, dedup. Pure and unit-testable |
| `app/extraction/patterns/` | One module per listing format (JSON-LD, WordPress, ICS, ...), registered in `registry.py` |
| `app/scheduler/` | The background process (`python -m app.scheduler`) |
| `app/core/` | Cross-cutting helpers: CSRF, templating, security, logging, URL safety |
| `scripts/` | One-off admin scripts, run from this folder |
| `tests/` | pytest suite; `tests/fixtures/extraction/` holds saved source pages |

## Conventions

- Pages are rendered with `app.core.templating.render(request, "page.html", {...})`,
  which adds the CSRF token and flash message. Templates are in `../frontend/templates`.
- Every POST form is CSRF-protected; admin routes depend on
  `require_permission("...")` from `app.services.rbac`, and admin actions are audited.
- Extraction dispatch always goes through `PatternRegistry`. Never special-case a
  site by hostname; a site's behaviour is data in its `SiteConfiguration`.
- Outbound fetches go through the SSRF-safe fetch layer (`app/extraction/fetch.py`,
  `app/core/url_safety.py`), never raw `httpx`/`requests` calls to user-supplied URLs.
- The web process never starts the scheduler or scrapes on startup.
- Optional features (AI categorization, geocoding, email, browser fetch) are off by
  default and must never break extraction or public pages when unconfigured.
- Tests use their own SQLite DB (`tests/conftest.py`); network access in tests is mocked.
- Lines up to 100 chars; Ruff rules E, F, I, UP, B, SIM (`pyproject.toml`).

## Deeper docs

`../docs/architecture.md`, `../docs/extraction-patterns.md`,
`../docs/automatic-onboarding.md`, `../docs/scheduler.md`, `../docs/security.md`,
`../docs/operations.md`, `../docs/deploy-render.md`.
