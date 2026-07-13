# New App (Phase 2: Local auth & RBAC)

Replacement city events application. Phase 1 established the database, models,
and a minimal FastAPI skeleton. Phase 2 adds local development login,
session-based auth, and role-based access control (see below). **Still no
scraping, scheduling, OAuth, or LLM integration.**

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

## Authentication & authorization (Phase 2)

**Login URL:** `/auth/login` (GET renders the form, POST submits it). **Logout:** `POST /auth/logout`.

This is **local email/password login — a development/fallback mechanism**,
clearly labeled as such in the UI. It's controlled by `LOCAL_LOGIN_ENABLED`
(default `true`) and is meant to be turned off once an external identity
provider (Google/Microsoft/Facebook, added in a later phase) is wired up. The
role/permission layer underneath is provider-agnostic and will be reused
as-is by OAuth logins.

Create the first Super Administrator:

```bash
python scripts/create_superadmin.py --email admin@example.com --password "..."
```

**Protected pages auto-redirect to login.** Any admin page under `/admin` requires
a valid session. How an unauthenticated request is handled depends on what it
looks like it's asking for:
- A **browser navigation** (an `Accept: text/html` request — i.e. someone typing
  a URL or clicking a link) gets a `303` redirect to
  `/auth/login?next=<original path>`, and logging in from there sends you back
  to that original page.
- An **API/XHR-style request** (no `text/html` in `Accept`) still gets a plain
  `401 {"detail": "..."}` JSON response — this is what a future REST API or
  fetch()-based frontend should expect.

Default roles: Super Administrator, Administrator, Editor, Viewer — seeded
automatically by the Phase 2 migration (`app/core/permissions.py` is the single
source of truth for the roles/permission catalog). A Super Administrator can
manage roles, permissions, and user role assignments at `/admin/roles` and
`/admin/users`; the system refuses to remove the last active Super
Administrator's admin access. All resulting changes (login/logout, role and
permission changes, user activation/deactivation) are recorded in `audit_logs`,
viewable at `/admin/audit`.

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
