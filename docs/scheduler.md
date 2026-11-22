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
- **One import at a time, saved in batches**: `MAX_CONCURRENT_RUNS` is 1
  because a large calendar can use a few hundred MB while it imports and the
  Render Starter instance has 512 MB. An import saves its events in batches of
  `IMPORT_COMMIT_BATCH_SIZE` (50) with one commit each, rather than a commit per
  step per event, so it finishes sooner and holds the SQLite write lock only
  briefly at a time. Pages keep reading meanwhile (WAL), and a write from the
  site waits a fraction of a second at most. SQLite runs with
  `synchronous=NORMAL`, which syncs the disk at checkpoints instead of on every
  commit; a killed process loses nothing.
- **Daily clean-up sweeps, one at a time**: the home-town and attendability
  sweeps (which take down events their filters now leave out) run on start and
  then daily. They read the active events in batches of `SWEEP_BATCH_SIZE`
  (500, `app/services/event_sweeps.py`) and commit each batch, so their memory
  stays at a few tens of MB however large the events table is (about 300 MB
  each with 50,000 events before batching). After a start or a takeover they
  wait 2 and 7 minutes, so they do not land on top of the new process's first
  ticks, and a lock makes the second one wait if the first is still running.
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

## Daily check baseline

Every active, approved source should complete a successful check at least once
every 24 hours (a busy source may run every 12 hours when there is a clear
reason). `app/services/source_freshness.py` measures each source against that,
read-only, from run records, the scheduler job row and `schedule_config`:

- **Admin > Websites** shows each source's last successful check, next
  scheduled check, latest failure (and whether it has since recovered), and
  its daily-check status. A source more than 12 hours past its expected
  interval is marked **Overdue** (a source with automatic imports off is held
  to the daily baseline; one never checked is measured from its approval). The
  source that is holding its town's "Listings checked" time back is marked
  **Holds back town time**. The **Needs attention only** filter lists every
  source that is overdue, failing, or not on a daily schedule.
- **Admin > Websites > Daily checks** (`/admin/websites/daily-checks`) sorts
  sources into: checked daily, no schedule, automatic imports switched off,
  checked less often than daily, and failing. An administrator ticks sources
  and presses the button to put them on the 24-hour schedule. Nothing changes
  without that press, so a schedule an administrator chose (off, or longer
  than daily) is reported before it is replaced. Each change keeps the
  source's retry and backoff settings, spreads first checks over the next 24
  hours, and is audited as `schedule_daily_baseline`. Failing sources are never
  switched on or sped up there, and blocked, archived and unapproved sources
  are never scheduled. Approved configurations and events are not touched.

## Freshness shown to visitors

`app/services/event_freshness.py` turns run records into two public timestamps.

- **Town pages** ("Listings checked ..."): the oldest of each public source's
  latest successful persistent run (`manual`/`scheduled`, status `success` or
  `partial`). Only sources whose events can be public count (active, not
  archived, approved). A source that has never completed a successful check
  can't date the town, so the page says how many have not completed a first
  check yet; a source whose latest run failed keeps its last good time and is
  mentioned on the page too, and preview or detection runs never count. Page views
  never change it. When the source that sets the time is failing or overdue
  while the others are current, the page names it ("... could not be checked
  on the latest try" or "... has not been checked since then") and says when
  every other source was checked; administrators get a link to that source.
- **Event pages** ("Event information last updated ..."): `Event.content_updated_at`,
  set when an event is first listed and moved only when what a visitor sees
  changes (`visitor_snapshot`): from a check, an admin correction or a new AI
  category. It is not `scraped_at` (last seen by a check) or `updated_at` (last
  database write). Events listed before it existed have no value and show no line.

Both are formatted in the town's (or event's) IANA zone with the abbreviation in
force at that instant, so EST and EDT are right on either side of a DST change.
Each persistent run also records `events_changed`, `events_cancelled` and
`events_missing` (upcoming events this source no longer listed; counted only,
never hidden) next to `events_inserted` and `events_updated`.
