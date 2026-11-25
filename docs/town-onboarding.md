# Town onboarding runbook

How a new college town goes from "requested" to "routine monitoring" in
Bulletin. It is written for whoever runs onboarding (an administrator or Claude)
and ties together the pieces that already exist:

- the daily college town routine in the "Find event sites with Bright Data"
  project thread, which researches one town a day;
- the research workspace in the project files (`event-sites/`: the spreadsheet,
  `requested-towns.md`, `brightdata-handoff.md`, `daily/progress.md` and
  `tools/day.py`);
- the repository import (`backend/data/college_towns/`, see its `README.md`);
- the app's onboarding pipeline (`docs/automatic-onboarding.md`) and scheduler
  (`docs/scheduler.md`).

This runbook does not replace the routine. The routine owns the research and
the town CSV; this document is the checklist around it and the definition of
what each approval covers.

## Ground rules

These hold at every stage. When a stage below seems to say otherwise, these win.

1. **Nothing reaches production without the owner's approval of that step.** A
   push to `main` deploys (Render auto-deploys every push) and queues
   onboarding. Approving, activating, scheduling, blocking, archiving and
   marking duplicates are each a separate approval.
2. **No new paid services.** Bright Data is used only by the existing research
   routine, for search and for opening candidate listing pages. It is never used
   to import events, never to get past a site that refuses Bulletin (403,
   Cloudflare, a bot wall), and never added to a source's configuration.
3. **Never bypass a block.** A source that refuses Render goes to Blocked with
   its reason kept. One normal, read-only visit in a person's browser is the
   furthest a check goes, and only when the owner approves it.
4. **Browser extraction stays off.** Production runs with
   `BROWSER_EXTRACTION_ENABLED=false` on a 512 MB instance; Chromium launches
   caused the Nov 24 out-of-memory restarts. A source whose detected
   configuration needs the browser is never previewed or activated; it waits for
   a feed or a plain-HTTP alternative.
5. **Never delete or archive events, and never weaken extraction safeguards**
   to make a source pass. Keep administrator corrections, overrides and
   confirmed duplicate links.
6. **One deploy at a time.** Merge or push one change, let the deploy and both
   delayed startup sweeps finish, and verify production before the next.
7. **Commits use the owner's Git identity**, with no AI attribution, and no
   em dashes anywhere (see `CLAUDE.md`).

## Stages at a glance

| Stage | Who acts | Production change? | Approval needed before |
|---|---|---|---|
| 0. Pick the town | Routine | No | Nothing |
| 1. Research sources | Routine (Bright Data search and page reads) | No | Nothing |
| 2. Research package | Routine | No | Nothing; the package is the request |
| 3. Deployment gate and push | Routine | Yes: deploy, town created, sources queued | Owner approves the package and the push |
| 4. Onboarding results package | Claude reads the admin, read-only | No | Nothing; the package is the request |
| 5. Approve, activate, block | Admin action | Yes | Owner approves per source |
| 6. One manual import each, then schedules | Admin action | Yes | Owner approves the imports and the exact times |
| 7. First scheduled runs | Claude reads runs, read-only | No | Nothing |
| 8. Routine monitoring | Daily checks and inbox review | Only as approved | Per change |

The routine in its current form still pushes straight to `main` after stage 1
(the owner's standing instruction from November). Stages 2 and 3 describe the
approval-first flow recommended by the readiness audit (workstream G). Switching
the routine over is the owner's decision and is made in the Bright Data thread,
which owns the routine. Until then, stages 4 onward apply as written, and the
deployment gate in stage 3 applies to every push.

## Stage 0: pick the town

1. Read `event-sites/requested-towns.md`. If anything is under "Waiting", take
   the first line. It gets the next unused Day number, added as a new row on the
   Towns tab of `event-sites/college-town-event-sites.xlsx`.
2. Otherwise take the lowest Day number on the Towns tab not yet logged in
   `event-sites/daily/progress.md`.
3. Decide the town's record (one line each, all needed later):
   - **City name**: the municipality the events are in, unique among towns.
   - **State**: two-letter code.
   - **Slug**: `<town>-<state>` in lower case, for example `auburn-al`.
   - **Universities**: full names, several separated by `;`.
   - **Timezone**: an IANA zone such as `America/Chicago` or
     `America/Indiana/Indianapolis`. Never `EST`, `CST` or another
     abbreviation.
4. Town rules (from `brightdata-handoff.md`):
   - A campus that is not its own city uses the municipality around it
     (Chestnut Hill is Newton, SMU is Dallas).
   - No county-wide or regional visitor bureaus. A bureau's town-only
     sub-calendar is fine (Raleigh uses Visit Raleigh's Raleigh-only one).
   - Skip athletics calendars: they list away games.
5. Check the town is new: search Admin > Cities for the name and slug. If it
   exists, this is an expansion of an existing town; say so in the package and
   don't add a `cities.csv` row.

## Stage 1: research sources

Target: **at least 8 sources, at least 3 on campus and at least 3 in town**, up
to about 12. Fewer is acceptable only with a written reason (stage 2).

Where to look:

- **Campus**: the main events calendar, the student organization platform
  (CampusGroups, Presence, Engage), the student union or activities board, the
  performing arts center, campus museums, the music school.
- **Town** (that town only): the downtown district, the city calendar, the
  public library, music and arts venues and theaters, the arts council, the
  local newspaper's calendar.

Every source must be:

- the listing page itself, public, with upcoming events dated from today on
  (open it and confirm real dates);
- not Facebook, Instagram, X, Eventbrite, Meetup, a ticket seller, AllEvents or
  a PDF;
- **not already in Bulletin.** Search Admin > Websites for the source's host
  before adding it, and check the spreadsheet's All sources tab. The onboarding
  queue refuses exact duplicate URLs, but a different path on the same calendar
  (a venue page that is a filter of the main calendar) still overlaps. List any
  overlap in the package instead of silently adding it.

Prefer, in this order: an ICS or other published feed, a page with structured
data (Localist, LiveWhale, The Events Calendar, JSON-LD), then plain HTML with
dates. Note anything that will likely need a browser (content that only appears
after scripts run): it will not be activated.

If Bright Data refuses a page (an `X-Brd-Error`, .gov KYC, robots) mark it
needs-check rather than retrying.

Record the research with `tools/day.py <spec.json>` from `backend/`: it updates
the spreadsheet, writes `event-sites/daily/<day>-<slug>.csv`, validates it with
the app's `parse_csv`, and appends the day to `progress.md`. The CSV format is
`url,name,source_display_name,city_slug,timezone`.

## Stage 2: research package

Before anything is pushed, post the package below in the Bright Data thread and
add or update the town's row on the Project Workboard (Waiting on Me). Use the
template in [Package templates](#package-templates).

The package answers: which town record will be created, which sources will be
queued, why each is a real listing for this town, what overlaps, and what is
likely to fail. The owner approves, edits, or holds it.

## Stage 3: deployment gate and push

Only after the package is approved.

**Deployment gate** (all five must hold, checked on Render right before the
push; the owner's rule since Nov 24):

1. Memory has room: the web instance is well under 512 MB (record the number).
2. No out-of-memory restarts recently (none since the last deploy settled).
3. Exactly one healthy scheduler leader (`/health/ready` returns 200).
4. Both delayed startup sweeps of the last deploy have finished.
5. No other deploy is running or queued, and the coordinator confirms the
   deploy slot is free.

If any check fails, don't push. Report it and keep the CSV local.

**Push:**

1. Copy the CSV to `backend/data/college_towns/<day>-<slug>.csv`.
2. If the slug is new, append a row to `backend/data/college_towns/cities.csv`:
   `name,slug,state_or_region,universities,timezone`.
3. From `backend/`: `python -m pytest tests/test_college_towns.py -q` and
   `python -m ruff check .`.
4. Commit and push, then watch the deploy until both sweeps finish. Record peak
   memory, restarts and the onboarding batch number.

What the push does: on scheduler start the import creates the town (an existing
town is never changed) and queues the file once as an onboarding batch
(correlation id `college-towns:<file name>`, so a redeploy never queues it
twice). Each job fetches the page, detects the pattern, drafts a configuration
and previews it without saving events. Automatic approval and activation are
off, so every source waits for review. A source whose first fetch is refused
still gets a Website, in Blocked or Draft, inactive and unscheduled, with the
reason kept.

## Stage 4: onboarding results package

Read-only. Open the batch (`/admin/onboarding/batches/<id>`) and each Website it
created (`/admin/websites/<id>`), and write the results package (template
below). For each source record:

- its status (Detected, Needs review, Blocked by site, Unsupported, Draft);
- the detected type and format, and whether it needs the browser;
- the preview: found, valid, rejected, with the reason for rejects;
- duplicates: events that match ones already in the town, and overlap with
  another source in the same batch;
- geography: events dropped or flagged as out of town;
- not attendable: deadlines, closures, staff-only items;
- dates and times: spot-check two or three events against the source page for
  the right day, time and timezone, and for all-day events;
- venue: whether events carry a venue or address;
- a verdict: **Ready to approve**, **Fix the configuration first** (say what),
  **Blocked** (say the response), or **Unsupported** (say why).

Then list Blocked and Unsupported sources apart from the rest, and, if fewer
than 8 sources are ready, say why and what could replace them.

Watch for:

- **Self-duplicates.** A job killed mid-run starts over and can match the
  Website it already created (Jule Collins Smith Museum, Nov 24). Report it; do
  not retry the job until that is fixed.
- **Browser strategy detected.** Never preview it. Record it as needing a feed.
- **Page or event caps.** A run that stopped at the limit shows "Listing read
  to the end: No" and sends a notification. Report it with the count.

Don't change configurations, approve or retry anything in this stage. Draft
configurations may be previewed again (previews save nothing) when the package
needs the numbers.

## Stage 5: approve, activate, block

Only the sources the owner names, in the way they name. Typical decisions:

- **Approve and activate with automatic imports off.** Approval freezes the
  configuration snapshot; activation makes the source eligible for scheduling.
  Keeping imports off at first lets one manual import be checked before a
  schedule exists.
- **Move to Blocked**, keeping the reason (for example a Cloudflare 403).
- **Leave Unsupported** with an investigation note; it stays visible under
  Problem.
- **Prepare a corrected draft** and preview it, then come back with the numbers.
  A corrected draft is never approved in the same step.

After acting, re-read each Website and confirm its status matches what was
approved.

## Stage 6: one manual import each, then schedules

1. With approval, run one manual import per active source
   (`/admin/websites/<id>`, run now) and report found, rejected, new, updated,
   duplicates skipped, out of town and not attendable. A partial run (for
   example one card with no title) is reported, not retried.
2. Propose a daily slot for each source, staggered about 20 minutes apart and
   away from the evening window where most existing daily runs sit. Give the
   times in Eastern time and in the town's local time.
3. With approval of the exact times, turn each schedule on (24-hour interval,
   `interval_minutes: 1440`). **A daily slot is fixed by the moment the schedule
   is turned on**, so turn each one on at its approved time. The scheduler runs
   a fixed interval in UTC, so local run times shift by an hour at daylight
   saving changes; record the next change date on the source follow-up list.
4. Confirm each source has **exactly one** enabled scheduler job.

## Stage 7: first scheduled runs

Read-only, the day after schedules start:

- every run's status and counts, with any failure or page-cap warning;
- Render memory through the runs and any restarts;
- public check: the town appears on the home page (towns with no upcoming
  events are hidden), its page lists the events, the "Listings checked" line is
  current, and each duplicate shows once;
- new duplicates (for example a feed that publishes each date twice). List the
  exact pairs for review; mark them `confirmed_duplicate` only after approval;
- the AI labeling backlog: new events join the queue, and Groq's free daily
  token limit sets the pace. Report the added count; don't change labeling
  settings.

When every run passes, the town moves to routine monitoring. Mark the Workboard
row Completed with the date, what was verified and any open items.

## Stage 8: routine monitoring

- Every active, approved source is checked at least every 24 hours
  (`/admin/websites/daily-checks` and Admin > Websites freshness columns).
- New problems arrive as admin notifications and are reviewed in the evening
  inbox review.
- A source that fails three runs in a row becomes Failing. Its handling is
  being changed in the "Keep failing sources' events visible" workstream (its
  events should stay visible and the town should show the source could not be
  checked); until that ships, report it rather than reactivating by hand.
- Paused sources with a known reason and a recheck date go on
  `source-review/follow-ups.md` in the project files.

## What needs approval

| Action | Allowed without asking | Needs the owner's approval |
|---|---|---|
| Research with Bright Data search and page reads, inside the routine | Yes | |
| Write CSVs, spreadsheet rows and progress logs in the project files | Yes | |
| Read admin pages, batches, runs, Render metrics and logs | Yes | |
| Preview a draft configuration (saves no events) | Yes | |
| Push a town to `main` | | Yes, and only after the gate passes |
| Approve, activate, deactivate, block or archive a source | | Yes, per source |
| Change an approved configuration | | Yes |
| Manual import, schedule on or off, schedule time | | Yes, with exact times |
| Mark duplicates, change or hide events | | Yes, with the exact ids |
| Any extra request to a site that blocks Bulletin | | Yes, one visit at a time |
| Change environment variables or the routine itself | | Yes |
| Any new paid service or paid Bright Data use | | Not planned; ask first |

## Where things are recorded

- Research: `event-sites/college-town-event-sites.xlsx`, `event-sites/daily/`,
  `event-sites/daily/progress.md`, `event-sites/requested-towns.md` (moved to
  Done with the date).
- Repository: `backend/data/college_towns/`.
- Packages and decisions: the Bright Data thread, plus one Project Workboard row
  per town.
- Open items and recheck dates: `source-review/follow-ups.md`.

## Package templates

Copy these into the thread. Keep numbers exact and say when something was not
checked.

### Research package (stage 2)

```markdown
**Town package: <City>, <ST> (Day <N>)**

City name: <City>
State: <ST>
Slug: <slug>
Universities: <University one>; <University two>
Timezone: <IANA zone>
New town: yes | no (existing town <id>, adding sources)

| # | Source | Campus or town | URL | Format | Upcoming dates confirmed | Overlap or note |
|---|---|---|---|---|---|---|
| 1 | <name> | Campus | <url> | ICS / Localist / JSON-LD / HTML | yes, <first two dates> | <none, or overlaps #n> |

Counts: <n> sources, <c> campus, <t> town.
Likely to fail: <source and why, for example a Cloudflare page, needs a browser>.
Left out: <source and why, for example athletics, county bureau, Facebook only>.
Fewer than 8: <only if it applies: why, and what was tried>.
Already in Bulletin: <none, or Website ids checked>.

Asking for: approval to push this town once the deployment gate passes.
```

### Onboarding results package (stage 4)

```markdown
**Onboarding results: <City>, <ST>** (batch <id>, deploy <commit>, peak memory <MB>, restarts <n>)

| Website | Source | Status | Type | Found | Valid | Rejected | Duplicates | Out of town | Not attendable | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| <id> | <name> | Detected | Localist | 31 | 31 | 0 | 0 | 0 | 2 | Ready to approve |

Spot checks: <event, source date and time, Bulletin date and time, match yes or no>.

Blocked: <Website id, name, response (for example HTTP 403, Cloudflare "Just a moment...")>.
Unsupported: <Website id, name, why, what would fix it (feed, plain-HTTP page)>.
Needs a browser: <Website id, name> (not previewed).
Fewer than 8 ready: <why, and possible replacements>.

Asking for:
1. Approve and activate (imports off): <ids>
2. Move to Blocked: <ids>
3. One manual import each, then daily at: <id> <time ET> (<local time>), ...
```

## Related

- `docs/automatic-onboarding.md`: the pipeline, policies and bulk onboarding.
- `docs/scheduler.md`: schedules, the daily check baseline and freshness.
- `docs/extraction-patterns.md`: what each detected pattern reads.
- `backend/data/college_towns/README.md`: the file format the push uses.
