# Architecture

The application is a city-events aggregator: it onboards event sources,
extracts events on a schedule, and serves them publicly. It is built as a
FastAPI app plus a dedicated background process, over a single relational
database.

## Processes (see docs/deployment.md)

- **Web** (`app.main:app`) — HTTP: public event browsing, admin, auth. Starts
  **no** scheduler and makes no outbound scraping request on startup.
- **Scheduler** (`python -m app.scheduler`) — exactly one process. Runs the
  durable scheduler (refresh of approved sources) and drains the onboarding,
  geocoding, and alert queues. See `docs/scheduler.md`.
- **Browser worker** (optional) — the restricted Playwright fetch strategy for
  JS-rendered sources; bounded concurrency.

## Layers

- `app/routers/*` — HTTP endpoints (thin; auth/CSRF/permission gating).
- `app/services/*` — business logic; **the only layer that opens a DB session**
  for extraction (`extraction_runs.py`) and other write paths.
- `app/extraction/*` — the pure extraction engine: fetch → detect → extract →
  normalize → validate → dedup. No session, no network beyond the fetch
  strategy; independently unit-testable. Dispatch is **always** through the
  `PatternRegistry` — never hostname/site-name conditionals.
- `app/repositories/*` — data access.
- `app/models/*` — SQLAlchemy models; `migrations/` — Alembic.

## Extraction engine

`PatternRegistry` holds 11 patterns (wordpress_rest, json_ld_event,
generic_html_cards, the_events_calendar, livewhale_json, ics_calendar,
rss_atom_events, embedded_json, next_data, nuxt_payload, algolia_search). A
site's behaviour is entirely a validated `SiteConfiguration` (closed, plain
data — no executable content). Shared capabilities: date-range parsing,
recurrence expansion, and geographic filtering (Phase 8G). See
`docs/extraction-patterns.md`.

## Onboarding

Detect → propose → draft → preview → score → (policy) approve/activate. Fully
automatic onboarding is policy-gated and conservative by default. See
`docs/automatic-onboarding.md`.

## Optional, disabled-by-default subsystems

AI configuration assistance (8E), async geocoding (11), email alerts (13),
external OAuth identities (14), and AI enrichment (17) are all **off by
default** and require explicit configuration; the app is fully functional
without any of them, and none can fail extraction or public display.

## Data flow (public)

Only publicly-visible, active events of active websites/cities appear; a
recurrence parent is never shown, only its occurrences. Public coordinates
prefer an administrator correction, then the source, then a geocoded value.
