# Recovery & incident response

## Database restore

1. Provision an empty PostgreSQL database.
2. `psql "$DATABASE_URL" < backup.sql`.
3. `python -m alembic current` — confirm the revision matches the release.
4. Bring up `web` and exactly one `scheduler`.

## Rollback a release

1. Redeploy the previous image tag (web + the single scheduler).
2. Only if a migration must be undone: take a backup, then
   `python -m alembic downgrade <previous_revision>`. Additive migrations
   (the norm here) are safe to leave applied.

## Stuck / dead scheduler

- The scheduler holds a DB leader row with a heartbeat. If the process dies, a
  replacement claims leadership once the heartbeat goes stale
  (`STALE_LEADER_SECONDS`).
- Per-site locks are reclaimed automatically when their heartbeat is stale
  (`reclaim_stale_locks`), so a crash never leaves a site permanently
  "running". A manual nudge: restart the scheduler; startup runs `reconcile`.
- Verify via `/health/ready` (`scheduler.leader_fresh`) and `/admin/reports`.

## A source keeps failing

- Repeated **structural** failures trigger re-onboarding: detection is re-run,
  the draft refreshed and previewed, and reviewers are notified — the approved
  configuration is **never** silently replaced. Re-approve explicitly if the
  new draft is correct.
- Transient failures advance the consecutive-failure count; after the threshold
  the source moves to `FAILING` (paused) and notifies reviewers. Fix and
  re-activate.
- Pause/cancel/run-now via `/admin/scheduler`.

## Geocoding backlog or provider outage

- The provider circuit breaker opens after repeated failures; the drain stops
  early and resumes after cooldown. Failed events are `geocode_status=failed`
  and retryable via `/admin/geocoding` (per-event retry) — nothing is lost.

## Suspected secret exposure

1. Rotate the affected secret in the secret store.
2. Restart the affected processes (nothing caches secrets on disk).
3. For OAuth, rotate the provider client secret and update the env.
4. Review the audit log and `/admin/reports` for anomalous activity.

## Bad data / accidental publish

- Events are deactivated/archived, never hard-deleted by the engine; a
  re-scrape never reactivates an administratively deactivated event.
- Confirmed duplicates and archived events are excluded from public views.

## AI or email misbehaviour

- Both are disabled by default and isolated: an AI failure never affects
  extraction or public display; email sends nothing unless a real backend is
  configured. Disable by unsetting `AI_ENABLED` / `EMAIL_ENABLED` and
  restarting.
