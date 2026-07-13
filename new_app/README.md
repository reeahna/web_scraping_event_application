# New App (Account management)

Replacement city-events application built with FastAPI, SQLAlchemy, Alembic,
Pydantic Settings, Jinja2, SQLite, pytest, and Ruff. The current phase provides
database foundations, local authentication, RBAC, public self-registration,
account management, and scraper-first event review. Live scraping, scheduling,
OAuth, saved events, followed cities, alerts, geocoding, and LLM integration
remain deferred.

## Setup

```bash
cd new_app
python -m venv venv
venv\Scripts\activate
pip install -e ".[dev]"
python -m alembic upgrade head
uvicorn app.main:app --reload --port 8100
```

The new app uses its own port and database; it does not read or modify
`legacy_app/events.db`.

## Configuration

| Variable | Default | Description |
|---|---:|---|
| `APP_NAME` | `New City Events App` | Application name |
| `APP_ENV` | `development` | Environment label |
| `APP_PORT` | `8100` | App port |
| `DATABASE_URL` | `sqlite:///<new_app>/app.db` | SQLAlchemy database URL |
| `LOG_LEVEL` | `INFO` | Application log level |
| `LOCAL_LOGIN_ENABLED` | `true` | Enable shared local email/password login |
| `REGISTRATION_ENABLED` | `true` | Show and accept public registration |
| `MINIMUM_PASSWORD_LENGTH` | `8` | Minimum local password length |
| `REGISTRATION_RATE_LIMIT_PER_HOUR` | `20` | Development-only registration guard |

Copy `.env.example` to `.env` to override these values. Paths are resolved
relative to `new_app/`, not the shell's current directory.

## Authentication and registration

- Shared login: `GET/POST /auth/login`
- Registration: `GET/POST /register`
- Personal account: `GET /account`
- Logout: `POST /auth/logout`

Registration creates an active account with only the **Registered User** role,
creates a fresh authenticated session, and redirects to `/account`. Registered
User has zero effective permissions by default and cannot enter `/admin`.
Registration accepts only display name, email, password, and password
confirmation; role, permission, and account-state fields are rejected.

Email normalization strips leading/trailing whitespace and lowercases the
entire address before lookup and storage. It intentionally does not apply
provider-specific transformations such as Gmail dot stripping or plus-address
removal.

Set `REGISTRATION_ENABLED=false` to hide Create Account links and reject direct
registration GET and POST requests server-side.

After login, a valid same-application relative `next` path takes priority.
Otherwise, users with effective admin permissions go to `/admin`, while users
without them go to `/account`. Browser requests to protected pages redirect to
login; API-style requests receive JSON 401 responses.

Create the first Super Administrator with:

```bash
python scripts/create_superadmin.py --email admin@example.com --password "..."
```

## Roles and migration behavior

The seeded roles are Super Administrator, Administrator, Editor, and Registered
User. The migration renames Viewer in place when possible, retaining its role
ID and all `UserRole` assignments, and removes its old permission grants. If
both role names exist unexpectedly, assignments are merged deterministically
without duplicates. Downgrade restores Viewer and its frozen historical
permission set without consulting current seed code.

Only a Super Administrator may grant Administrator or Super Administrator.
All role assignments and removals are audited, and the last-active-Super-
Administrator safeguard remains enforced.

## Event review and lifecycle

The protected event workspace is at `/admin/events`. It supports search,
pagination, filters, read-only source details, review status, narrow category
and location corrections, duplicate review, and provenance placeholders. There
is deliberately no general event-create or event-update permission, route, or
form. Extracted title, description, URLs, website, dates, times, image, and
external source ID remain authoritative source values.

The lifecycle has two independent, purposeful dimensions:

- `is_active` controls eligibility for public display.
- `review_status` is either `needs_review` or `reviewed`.
- `archived_at` retains historical records outside the normal active set.

Archiving deactivates an event. Restoring removes `archived_at` but leaves the
event inactive so publication remains an explicit action. Only archived events
can be permanently deleted. A separate deleted state is not used because it
would overlap with archival. Administrators receive destructive permissions;
Editors retain the configured non-destructive view/review permissions; and
Registered Users receive none. Every action is checked server-side and audited.

Category overrides and corrected public location values are stored separately
from extracted values. Recategorization never erases an active administrator
override. Venue/address/coordinate corrections can be cleared, validate
coordinate ranges, reject unrelated fields, and record before/after audit data.

## Categories and deterministic rules

Administrators can manage the 14 idempotently seeded categories at
`/admin/categories` and deterministic rules at
`/admin/categorization-rules`. Referenced categories cannot be deleted, and
inactive categories cannot be newly assigned. No category IDs are hardcoded.

The fixed rule precedence is exact source mapping, administrator mapping,
website-specific mapping, venue rule, keyword rule, then `Other` (or
uncategorized when `Other` is inactive). Within one rule type, higher priority
wins and the database ID is the stable tie-breaker. Results include the rule,
confidence label, explanation, manual-review recommendation, and fallback
state. Rules contain data only—never executable Python—and regular expressions
are length-limited, compiled before storage, and reject dangerous constructs.

## Fingerprints and duplicate review

Fingerprint selection is deterministic: source website plus external ID wins;
otherwise a normalized canonical URL is used; otherwise the fingerprint uses
normalized title, occurrence date, start time, venue, and city. Text trims and
collapses whitespace and uses case-folding. URLs lowercase scheme/host, remove
fragments and trailing slashes, and sort query parameters. Dates and times use
their ISO forms. Matching fingerprints create persisted possible-duplicate
statuses but never merge, archive, or delete records automatically. Authorized
reviewers may persist a duplicate decision and preferred record.

City deletion remains blocked by unarchived events. Its impact screen offers
explicitly confirmed, permission-protected bulk archival and deletion of
already archived records. Those operations are transactional and audit affected
counts.

## Registration rate-limit limitation

The current guard is development-only. It keeps per-IP timestamps in the
current Python process, resets on restart, is not shared across workers, and is
not a production anti-abuse control. Before deployment, replace the internals
behind `app.services.rate_limit.check_registration_rate_limit` with a shared
store such as Redis or a database-backed limiter.

## Verification

```bash
python -m ruff format --check .
python -m ruff check .
python -m pytest
python -m alembic upgrade head
python -m alembic downgrade ac034c0f9ec1
python -m alembic upgrade head
```

Tests use `tests/test_app.db`, separate from the development and legacy
databases.
