# Scheduler and background processing (Phase 10)

Background scraping runs in a **dedicated scheduler process**, never inside a
FastAPI web worker. Running the scheduler per web worker would give every
worker its own scheduler, all racing over the same sources. The web app starts
no scheduler at all (see `app/main.py` lifespan).

## Startup commands

Development — run the web app and the scheduler as two processes:

```bash
# terminal 1: web app
uvicorn app.main:app --reload --port 8000

# terminal 2: the single scheduler process
python -m app.scheduler
```

Production — run exactly **one** scheduler process alongside however many web
workers you like:

```bash
# web (multiple workers is fine — none of them schedule)
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4

# scheduler — run ONE instance (e.g. its own systemd unit / container)
python -m app.scheduler
```

If a second scheduler process is started by mistake it claims the DB leader row
only if the current leader's heartbeat has gone stale; otherwise it stays idle.
This is a safety net, not a substitute for running a single instance.

## How it works

- **Durable state lives in the database**, not in APScheduler's jobstore. Each
  scheduled site has a `scheduler_job_state` row holding its next run, last
  status, a per-site lock (running + holder + heartbeat), a cancellation
  request, a pause flag, and a structural-failure counter. A restart reconciles
  from these rows and resumes; nothing is lost.
- **APScheduler** drives just two recurring ticks in the leader process: a
  leader heartbeat (also reclaiming stale locks and periodically reconciling)
  and a dispatcher that launches every due, eligible, not-already-running site.
- **Eligibility**: a site is scheduled only when it is `ACTIVE`, active, not
  archived, assigned to an active city, has a valid approved configuration, and
  has a valid, enabled `schedule_config`.
- **No overlap**: a per-site lock means a second fire for the same site while
  it is still running is skipped. If the holding process dies, its heartbeat
  stops; after `STALE_LOCK_SECONDS` any scheduler reclaims the lock, so a crash
  never leaves a site stuck.
- **Retries**: each run retries up to `schedule_config.max_retries` with bounded
  exponential backoff. Repeated failure advances the site toward the `FAILING`
  state through the existing extraction-health logic.
- **Structural failures → re-onboarding**: after several runs that fail
  structurally (hard failure or zero events extracted), the scheduler reruns
  detection, refreshes the draft configuration, previews it, and notifies
  reviewers with an approved-vs-detected comparison. It **never** silently
  replaces the approved configuration — re-approval stays an explicit, audited
  action.
- **Deactivation**: moving a website out of `ACTIVE` pauses its job
  immediately; deactivating a city pauses all its sources' jobs (enforced by
  eligibility and the periodic reconcile). Events and history are preserved.
- **Phase 8C onboarding** is drained from this same process (a third tick), so
  bulk onboarding is worker-compatible and does not run per web worker.
- **Async geocoding (Phase 11)** is drained from this process too (a fourth
  tick), and only when `geocoding_enabled` is set — the default makes no live
  geocoding request. Results go to the event's `geocoded_*` columns (never the
  immutable source coordinates or an administrator's correction), are cached by
  normalized-address hash, and Nominatim is rate-limited (>= 1s between calls)
  with a descriptive User-Agent per its usage policy. Admin controls live under
  `/admin/geocoding` (status, and a per-event retry that only requeues).

## Admin controls

Under `/admin/scheduler` (permissions `sites.view` to read, `sites.approve` to
act):

- `GET  /admin/scheduler/health` — leader freshness and scheduled/running/paused
  counts.
- `POST /admin/scheduler/websites/{id}/run-now` — schedule an immediate run
  (the scheduler process performs it; it never runs inline in the web request).
- `POST /admin/scheduler/websites/{id}/cancel` — request cooperative
  cancellation of the current/next run.
- `POST /admin/scheduler/websites/{id}/pause` and `/resume`.

## Schedule configuration

`Website.schedule_config` is validated by `app/schemas/schedule.py`:

```json
{
  "enabled": true,
  "interval_minutes": 1440,
  "jitter_seconds": 60,
  "max_retries": 2,
  "retry_backoff_seconds": 60,
  "retry_backoff_max_seconds": 1800
}
```

`interval_minutes` is floored at 15 minutes regardless of what a form submits,
so a misconfiguration cannot poll a third-party site aggressively.
