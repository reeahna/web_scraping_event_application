# New App (Phase 1: Foundation)

Replacement city events application. This phase establishes the database, models,
and a minimal FastAPI skeleton only — **no scraping, scheduling, auth, or LLM
integration yet.**

## Stack

FastAPI · SQLAlchemy 2 · Alembic · Pydantic Settings · Jinja2 · SQLite (swappable
to PostgreSQL later via `DATABASE_URL`) · pytest · Ruff

## Setup

```bash
cd new_app
python -m venv venv
venv\Scripts\activate        # Windows; source venv/bin/activate on macOS/Linux
pip install -e ".[dev]"

# Optional: copy and edit environment overrides
copy .env.example .env       # Windows; cp .env.example .env elsewhere

# Apply migrations
python -m alembic upgrade head

# Run the app (different port and database file from legacy_app)
uvicorn app.main:app --reload --port 8100
```

Then open http://localhost:8100 and http://localhost:8100/health.

## Environment variables

| Variable | Default | Description |
|----------|---------|--------------|
| `APP_NAME` | `New City Events App` | Display name |
| `APP_ENV` | `development` | Environment label |
| `APP_PORT` | `8100` | Default port (distinct from legacy_app's 8000) |
| `DATABASE_URL` | `sqlite:///<new_app>/app.db` | SQLAlchemy URL; point at a Postgres DSN later without code changes |
| `LOG_LEVEL` | `INFO` | Log level for the `app` logger |

All paths are resolved relative to `new_app/`'s own location (`app/config.py`'s
`BASE_DIR`), not the process's working directory — this avoids the
CWD-relative path fragility present in `legacy_app`.

## Database & migrations

- Models live in `app/models/`, one file per entity, registered on `app.database.Base`.
- Foundational tables (Phase 1): `users`, `roles`, `permissions`, `user_roles`,
  `role_permissions`, `cities`, `websites`, `event_categories`, `events`, `audit_logs`.
- SQLite foreign keys are explicitly enabled per-connection (off by default in SQLite).
- Migrations: `alembic upgrade head` / `alembic revision --autogenerate -m "..."`.
  `migrations/env.py` reads `DATABASE_URL` from the environment first, falling back
  to the app's own settings — this is what lets tests point Alembic at a throwaway
  database.

## Tests

```bash
python -m pytest
```

Tests use a dedicated `tests/test_app.db` (created/dropped per test), fully
separate from both the dev `app.db` and `legacy_app/events.db`.

## Linting & formatting

```bash
python -m ruff format .
python -m ruff check .
```

## Relationship to legacy_app

This app is being built alongside (not replacing yet) `../legacy_app/`, which
remains read-only and runs independently:

```bash
cd ../legacy_app
uvicorn main:app --reload --port 8000
```

Both apps can run at the same time — different ports (8100 vs 8000), different
databases (`new_app/app.db` vs `legacy_app/events.db`), different virtual
environments, and no shared scheduler (this app doesn't start one yet).
