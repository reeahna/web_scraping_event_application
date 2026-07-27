# Operations

## Environment variables

All configuration is environment-driven (`app/config.py`, pydantic-settings);
see `.env.example` for the full annotated list. Key groups: core
(`APP_ENV`, `DATABASE_URL`), security (`COOKIE_SECURE`, `BEHIND_HTTPS`,
`TRUSTED_HOSTS`, `MAX_REQUEST_BYTES`), rate limiting (`RATE_LIMIT_BACKEND`,
`REDIS_URL`), OAuth (`*_CLIENT_ID/SECRET`, `OAUTH_REDIRECT_BASE_URL`), geocoding
(`GEOCODING_*`), email (`EMAIL_*`), and AI (`AI_*`). Optional subsystems are all
disabled by default.

## Migrations, backup, restore, rollback

- Apply: `python -m alembic upgrade head`. Show: `alembic current`.
- **Backup** (Postgres): `pg_dump "$DATABASE_URL" > backup.sql` on a schedule;
  store off-box. **Restore**: `psql "$DATABASE_URL" < backup.sql` into an empty
  DB, then `alembic current` to confirm the revision.
- **Rollback**: redeploy the previous image; undo a migration only with a
  backup in hand: `alembic downgrade <previous_revision>`. See `docs/recovery.md`.

## Running the processes

Web, one scheduler, optional browser worker — see `docs/deployment.md` and
`docs/scheduler.md`. The scheduler is the single owner of all background work
(refresh, onboarding drain, geocoding drain, alert reminders/digests).

## Observability

- **Health**: `/health` (+ `/health/live`), **readiness**: `/health/ready`
  (DB, scheduler leader freshness, AI provider health, production blockers).
- **Operational dashboard**: `/admin/reports` (`reports.view`) — sources by
  status, onboarding queue, runs, validation errors, unsupported reports, drift
  proposals, geocoding failures, duplicate queue, recurrence truncations,
  geography exclusions, scheduler/AI health, and a paginated audit log. Safe
  fields only.
- **Structured logging + correlation IDs**: every request carries an
  `X-Correlation-ID`; audit rows and logs reference it. Configure log shipping
  at the platform level.
- **Error reporting**: unhandled exceptions return a redacted response and log
  with the correlation id; wire a platform error reporter to the logger.
- **Metrics**: derive from `/admin/reports` counts and `/health/ready`; none
  expose secrets.

## Retention policies

- **Events**: deactivated/archived events are retained (never auto-deleted);
  archiving is a prerequisite to deleting a city. Cancelled recurrence
  occurrences are hidden, not deleted.
- **Extraction runs / provenance / errors**: retain per your compliance window;
  prune with a scheduled `DELETE` on `extraction_runs`/`extraction_errors`/
  `event_provenance` older than the window (evidence retention).
- **Audit log**: retain for the compliance-required period; audits are
  append-only and never rewritten. Prune only by explicit, documented policy.
- **Geocode cache / alert deliveries**: retain; both are deduplication ledgers.

## Privacy & deletion

- Personal data is limited to registered-user accounts and their saved
  events / follows / alert preferences, each scoped strictly to the owner.
- **User deletion**: deleting a `users` row cascades to sessions, saved events,
  follows, alert preferences, and external identities (FK `ON DELETE CASCADE`).
- **Email unsubscribe**: token-based, no login required (`/alerts/unsubscribe`).

## Secret rotation & incident recovery

See `docs/security.md` (rotation) and `docs/recovery.md` (incidents,
stuck scheduler, failing sources, restore).
