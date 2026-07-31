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

## An unsupported source (no detector matched)

The primary recovery path stays **automatic**. When ordinary HTTP detection
returns `unsupported`/`needs_review`, an administrator can trigger
**restricted browser detection** from the website page (permission
`sites.test`, CSRF-protected, hidden unless `BROWSER_EXTRACTION_ENABLED` is on
and the site is not archived):

1. The listing is rendered once in the locked-down Playwright strategy
   (`app.extraction.browser`) — a closed action plan (no arbitrary JS), every
   navigation and subrequest SSRF-revalidated, downloads/popups blocked,
   challenge/login walls reported-not-solved, always torn down.
2. `app.services.browser_observation.render_and_observe` *classifies and scores*
   every response the page fetched (`app.extraction.structured_candidates`),
   not just the ones an existing detector recognises. Third-party telemetry
   (analytics, ad pixels, social beacons, map tiles) is filtered out by
   registrable-domain comparison; first-party JSON is scored on event-likeness
   (record arrays, event fields, ownership). It returns **one explicit
   outcome** — `structured_selected`, `structured_pattern_needed`,
   `rendered_selected`, `blocked`, or `no_source` — so recovery never has to
   infer the source from a contradictory mix of flags.
3. `app.services.browser_recovery` branches on that outcome:
   - `structured_selected` / `rendered_selected` → the ordinary
     propose → draft → preview → Phase 8D policy pipeline (no Event row; a draft
     is written; `configuration_version` bumps; approval/activation stay
     policy-gated). A recovered source lands in `needs_review` for a human.
   - `structured_pattern_needed` → a qualifying first-party event endpoint was
     found but no registered pattern can extract it. Recovery records the
     candidate analysis and keeps the source in `needs_review` **without**
     proposing `generic_html_cards`, writing a draft, running a preview, or
     bumping `configuration_version`. Equivalent retries are idempotent (a
     bounded candidate fingerprint gates re-recording; only an attempt counter
     advances), so a source cannot accumulate failed generic drafts.

Blocked outcomes (CAPTCHA, Cloudflare, login wall, SSRF, timeout) are recorded
on the source's unsupported-site report (`browser_recovery`, a bounded/redacted
summary — never cookies, headers, tokens, or full bodies) and never
circumvented. The advanced manual pattern selector (registry-driven, draft-only,
preview-and-approval still required) remains as a fallback.

## Timezones

Store IANA names (`America/Indiana/Indianapolis`), not fixed-offset
abbreviations. `EST` is a *recognised* zoneinfo name but a constant −05:00 that
ignores DST, so using it for a DST-observing location shifts summer events by an
hour. The admin UI shows a warning (`app.core.timezones.dst_warning`) whenever a
fixed abbreviation is configured; changing a stored timezone is an explicit edit
and never rewrites existing event timestamps.

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
