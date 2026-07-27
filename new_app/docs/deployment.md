# Deployment

This describes how to deploy; it does **not** perform a deployment. Do not
purchase hosting, create cloud accounts, configure DNS, issue certificates,
create live OAuth apps, or expose secrets without explicit authorization.

## Production prerequisites

Production is **not** ready while it depends on any of: SQLite, in-process rate
limiting, a scheduler per web worker, development cookies, or unbounded browser
workers. `/health/ready` lists remaining blockers (`app/core/production_checks.py`).

Required for production:

- **PostgreSQL** as `DATABASE_URL` (`postgresql://…`). Install `psycopg[binary]`
  (the Dockerfile does). Models/migrations use portable SQLAlchemy constructs;
  the partial unique indexes (auto-onboarding global default) already carry a
  `postgresql_where`.
- **Redis** for shared rate limiting (`RATE_LIMIT_BACKEND=redis`, `REDIS_URL`).
- `COOKIE_SECURE=true`, `BEHIND_HTTPS=true`, `TRUSTED_HOSTS=[...]`.
- TLS terminated by the reverse proxy; forward `X-Forwarded-*`.

## Processes

Run these as separate services (see `docker-compose.prod.yml`):

| Role | Command | Count |
|------|---------|-------|
| web | `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers N` | N workers |
| scheduler | `python -m app.scheduler` | **exactly 1** |
| migrate (one-shot) | `python -m alembic upgrade head` | per release |
| browser worker (optional) | scale-out of scheduler-owned browser work | bounded |

The scheduler owns a DB leader row; a second scheduler stays idle unless the
leader's heartbeat goes stale. The scheduler also drains onboarding, geocoding,
and alert queues, so a separate worker is only for further scale-out.

## Build & run (local production topology)

```bash
cd new_app
cp .env.example .env            # fill in real values; never commit .env
docker compose -f docker-compose.prod.yml up --build
```

`migrate` runs `alembic upgrade head` before `web`/`scheduler` start.

## Migrations

```bash
python -m alembic upgrade head          # apply
python -m alembic downgrade -1          # roll back one (see docs/recovery.md)
python -m alembic current               # show current revision
```

Never run `downgrade` against production data without a backup and approval.
CI round-trips every migration on a scratch DB.

## Playwright

The browser worker needs chromium: `python -m playwright install chromium`
(the Dockerfile runs `--with-deps chromium`). The web process does not need it.

## OAuth callbacks

Set `OAUTH_REDIRECT_BASE_URL` to the public HTTPS origin; each provider's
callback is `{base}/auth/oauth/{provider}/callback`. Providers stay disabled
until both client id and secret are set. Register callbacks only with
authorization.

## Health checks

- Liveness: `GET /health/live`
- Readiness: `GET /health/ready` (DB, scheduler, provider, production blockers)

## Rollback

Redeploy the previous image tag. Only if a migration must be undone, run
`alembic downgrade <previous_revision>` after a backup. See `docs/recovery.md`.
