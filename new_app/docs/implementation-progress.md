# Implementation progress

Living record of phase-by-phase completion for New City Events (new_app only).
legacy_app is never modified.

## Baseline at start of this run

- Branch: `main`
- Last commit before this run: `366373b` (fix: prefer non-archived website matches)
- `app.db` Alembic revision at baseline: `b2f4a7c91d05` (Phase 8C head)
- Migration head on disk at baseline: `d7a1c4e83b60` (Phase 8D, not yet applied to app.db)
- Registered extraction patterns (5): `wordpress_rest`, `the_events_calendar`,
  `livewhale_json`, `json_ld_event`, `generic_html_cards`
- Incomplete phase at baseline: Phase 8D (increments 1–5 implemented but uncommitted;
  three deferred items outstanding)

## Known limitations carried in

- Shared-year date ranges (e.g. `Sep 29 - 30 / 2026`) are rejected, owed to Phase 8G.
- Batch-selected policy override was deferred within 8D increments 1–5.
- Success-outcome notifications and job/batch decision links were deferred within 8D.

---

## Phase 8D — policy-controlled automatic onboarding

**Status:** complete.

**Summary.** Persisted automatic-onboarding policies with a conservative seeded default
(approval and activation off, generic HTML/browser/AI-origin denied). A pure decision
service evaluates each source against a policy snapshot; approval executes through the
existing `approve_configuration` and activation through `transition_website`, with a
post-approval re-check. Decisions are append-only; actions are recorded as separate
action-result rows. System actions are audited with a distinct actor type and no login
account. Policy administration UI with confirmation phrases; decision-explanation UI;
reevaluation; manual approval remains available after a policy denial.

**Completion items added in this commit (the three deferred from increments 1–5):**

- Batch-level policy override: `onboarding_batches.selected_policy_id`; resolution
  precedence is now batch → city → global. Selecting a policy that enables approval or
  activation requires `settings.manage` (enforced in the submit route, not just hidden);
  the selector is shown only to settings managers.
- Success-outcome notifications: `onboarding_batch_auto_approved` and
  `onboarding_batch_auto_activated`, one summary per batch, fingerprint-deduplicated.
- Decision links on the onboarding job and batch detail pages; selected-policy row on the
  batch page.

**Components:** `app/core/auto_onboarding.py`; models `auto_onboarding_policy`,
`auto_onboarding_decision`, `auto_onboarding_action_result`;
`repositories/auto_onboarding.py`; services `auto_onboarding_decision`,
`auto_onboarding_persistence`, `auto_onboarding_execution`, `auto_onboarding_reevaluation`,
`auto_onboarding_policies`; router `auto_onboarding_policies` (+ decisions router);
templates under `admin/auto_onboarding/`; audit actor columns; `websites.configuration_origin`.

**Migration:** `d7a1c4e83b60` (revises `b2f4a7c91d05`). Adds 4 tables + audit actor columns
+ `websites.configuration_origin` + `onboarding_batches.selected_policy_id`; seeds the
conservative default idempotently. Round-trip (upgrade → downgrade → upgrade) verified on an
isolated scratch database; unrelated rows preserved; seed does not duplicate.

**Deferred / not built (documented, not silently dropped):**

- "Force manual review" rollback control — not in the master 8D completion checklist and a
  clean implementation is constrained by the lifecycle transition table; deferred.
- A portion of the originally-enumerated UI-render test cases; core behaviour is covered.

**Preserved boundaries:** authoritative Website matcher unchanged; archived Websites never
qualify; automatic-policy evaluation never persists Event rows; approval leaves a site
inactive; approval and activation are separate service calls and separate audits.

**Verification:** targeted Phase 8D suites (models, qualification, execution, admin,
reevaluation, batch policy) pass; full suite **827 passed, 0 failed** (~19 min); Ruff clean;
migration round-trip verified on scratch DB. `app.db` upgraded `b2f4a7c91d05` → `d7a1c4e83b60`.

**Commit:** `10910b0` — feat: add policy-controlled automatic onboarding.

---

## Phase 8E — optional AI-assisted configuration fallback

**Status:** complete.

**Summary.** When deterministic inference cannot produce a reliable draft, an optional AI
provider may *suggest* a configuration. AI is a configuration assistant, never the scraper:
the suggestion is schema-validated and safety-checked, stored only as a draft with
`configuration_origin = ai_suggested`, previewed deterministically, and gated by the same
Phase 8D policy (which denies AI-origin approval by default). The whole app works with AI
disabled — the default — and recurring extraction after approval never calls the provider.

**Components:** `app/services/ai/` (`types`, `provider` with `DisabledAIProvider` +
`EchoAIProvider` + process-local budget/circuit-breaker, `evidence`, `suggestion`);
`app/services/ai_configuration.py` (orchestration reusing detection, preview, and the 8D
decision/execution services); AI settings in `config.py` (disabled by default, no key needed);
`POST /admin/websites/{id}/ai-suggest` + usage/suggest panel on the onboarding review page.

**Safety.** No network adapter exists — only `disabled` and an in-process `echo` for
tests/dev; an unknown provider name falls back to disabled. Evidence is bounded and sanitized
(`<script>`/`<style>`/`<iframe>` and non-structural attributes stripped, href query strings
removed, no headers/cookies/IPs/personal data/full document). The suggestion validator
requires the restricted `SiteConfiguration` schema (`extra="forbid"` rejects
instruction-shaped keys), a registered+allowed pattern, SSRF-safe URLs, no request headers or
body, and bounded pagination/detail limits. Nothing from a suggestion is executed. A drafted
suggestion persists no Event rows and is never approved or activated by this path.

**No migration.**

**Verification:** 16 targeted tests pass (validator rejections, disabled-by-default, echo
end-to-end draft-not-approved, no event persistence, provider failure, budget limit); Ruff
clean; full suite: see below.

**Commit:** `965e04f` — feat: add optional AI configuration fallback.

---

## Phase 8F — remaining structured extraction patterns

Delivered in two verified commits because the phase is large (six new adapters,
one atomic set would be an unreliable single stretch). Both parts are coherent
units: the JSON-in-script family (no new dependency, shared infrastructure),
then the parser/API adapters.

### Part 1 — JSON-in-script family: `embedded_json`, `next_data`, `nuxt_payload`

**Status:** complete.

**Summary.** Three patterns that parse strict JSON already in the page and read
the event list from a config-provided or discovered `events_root`. Nothing is
ever executed: `embedded_json` reads `<script type="application/json">`,
`next_data` reads the `__NEXT_DATA__` block, `nuxt_payload` reads a
`_payload.json` body or the `__NUXT_DATA__` script — and a Nuxt page whose
state is only a `window.__NUXT__ = (function…)` assignment is marked
`browser_required` (deferred to Phase 9), never eval'd. A shared, bounded,
deterministic finder (`inference/json_events.py`) locates the event array by
*scoring* every array of objects on event-like keys (title + date), so a nav
array is never mistaken for events; the proposers reuse it to discover
`events_root` and map fields from a real sample object.

**Components:** `patterns/embedded_json.py`, `patterns/next_data.py`,
`patterns/nuxt_payload.py`; `inference/json_events.py`;
`inference/proposers/json_scripts.py`; three detectors + reliability-order
entries in `detection.py`; registry wiring (registry now holds 8 patterns).

**No migration.** Registry now: wordpress_rest, the_events_calendar,
livewhale_json, json_ld_event, next_data, nuxt_payload, embedded_json,
generic_html_cards.

**Verification:** 16 targeted tests (finder scoring, detection incl.
browser-required, extraction, no-guess-without-root, proposer discovery, no
hostname branching) + affected suites (registry, detection, onboarding review,
inference, qualification) pass; Ruff clean; full suite: see below.

**Commit:** `602e641` — feat: add JSON-in-script extraction patterns.

### Part 2 — parser/API adapters: `ics_calendar`, `rss_atom_events`, `algolia_search`

**Status:** complete. Registry now holds **11 patterns**.

**Summary.** `ics_calendar` parses a VCALENDAR with the maintained `icalendar`
library — folded lines, all-day (`VALUE=DATE`) events, TZID, cancelled events
(flagged, never dropped), and recurrence rules preserved verbatim for the
Phase 8G expander; UID is the identity, so events without a public URL still
validate and dedup (canonical_url is not required). `rss_atom_events` parses
RSS/Atom with `defusedxml` (entity-expansion/external-entity attacks refused),
handles both `<item>` and `<entry>` by local name, and — deliberately — never
uses the publication date as the event date: the start comes only from a
configured element, so a generic news feed lands in review. `algolia_search`
maps a query response's `hits`; querying authenticates via a **secret
reference** (`fetch.secret_header_refs`, `env:NAME`) resolved at request time
and never stored, logged, or audited — a raw key in that field is rejected by
the schema. Its proposer refuses to propose an approvable config without a key
reference (per "if safe key handling cannot be proven, leave needs_review").

**Shared-core change:** `FetchConfig.secret_header_refs` + resolution in
`HttpFetchStrategy` (`app/services/secrets.py`), so a credential can reach an
outbound header without ever living in a `SiteConfiguration`.

**Dependencies added:** `icalendar>=6.0`, `defusedxml>=0.7` (declared in
pyproject; installed in the venv).

**No migration.**

**Verification:** 22 targeted feed/Algolia tests (incl. secret-ref resolution
via mocked transport, raw-key rejection, publication-date-never-used) +
affected registry/detection/SSRF suites pass; Ruff clean; full suite: see
below. A greedy-JSON detection bug (nuxt claiming any JSON body, incl. an
Algolia response) was found and fixed — nuxt detection now requires the
`__NUXT_DATA__` marker.

**Commit:** `3b41497` (part 2); `602e641` (part 1, JSON-in-script patterns).

## Phase 9 — restricted headless-browser fetch strategy

**Status:** complete.

Adds a Playwright chromium fetch strategy for client-rendered pages that plain
HTTP cannot resolve. It is a *fetch strategy*, not a scraper: it renders the
page, captures the rendered HTML plus any JSON the page fetched, and hands both
to the ordinary PatternRegistry detection/extraction — no site-specific logic,
no new dispatch path.

**Closed action plan (`app/schemas/browser.py`).** A browser plan is data, not
code: a discriminated union of `wait_for_selector`, `network_idle`, `click`,
`load_more`, `scroll`, and `dismiss_banner` — no field anywhere accepts a
Python/JS snippet, an `evaluate` expression, a path, or a command. Every count
and timeout is capped by a validator (`_MAX_TIMEOUT_MS`, `_MAX_CLICKS`,
`_MAX_SCROLLS`, `_MAX_ACTIONS`, `_MAX_TOTAL_MS`), so a plan cannot scroll or
wait forever. `dismiss_banner` only closes an overlay — it never accepts terms
or grants consent.

**Strategy (`app/extraction/browser.py`).** Safety is enforced in the strategy,
not trusted to the caller: the initial URL is SSRF-validated before launch, and
a route interceptor re-validates every subrequest — private/loopback/non-http(s)
targets are aborted, so a rendered page cannot pivot to internal services.
Downloads, popups/new windows, and service workers are disabled; media is
blocked by default. A challenge/login wall (Cloudflare, CAPTCHA, "checking your
browser", "sign in to continue") or a 401/403/429 is detected and reported as
`blocked_reason` — never solved, bypassed, or submitted to. The browser,
context, and pages are always torn down in a `finally` block. The SSRF host
check is indirected through `_host_allowed` so tests can permit a local fixture
server without weakening the production default.

**Observation (`app/services/browser_observation.py`).** `render_and_observe`
runs the existing `run_detection` over both the rendered HTML and each observed
JSON response, and prefers a structured API: if the page's own JSON endpoint
detects as an event source it is chosen over the rendered HTML, because a
reusable API is cheaper and more stable than re-rendering on every scheduled
run (`chosen_source` = `structured_api` | `rendered_html`).

**Dependency added:** `playwright>=1.47` (declared in pyproject; installed in
the venv, `playwright install chromium` run).

**No migration.**

**Verification:** 9 Phase 9 tests drive the *real* headless chromium against a
local fixture HTTP server (no live third-party requests, per the master
prompt): JS-rendered cards captured and detected as `generic_html_cards`; a
page's Algolia-shaped JSON endpoint preferred over rendered HTML; a challenge
page reported blocked (nothing captured); a private URL refused before launch;
plan schema rejects unknown actions and enforces caps; a second render succeeds
(clean teardown). Tests skip with a clear reason if chromium is unavailable, so
the suite stays green on machines without the binary. Ruff clean; full suite:
889 passed.

**Commit:** `7789b27`.

## Phase 8G — shared date ranges, recurrence, and geography

**Status:** complete (completes Phase 8).

Three shared-core capabilities, each one function/service every pattern calls —
no per-pattern or per-site copies.

**Date ranges (`app/extraction/date_ranges.py`).** A closed parser for the
explicit multi-day forms (`Sep 29 - 30 / 2026`, `Sep 29–30, 2026`,
`September 29 - October 1, 2026`, `Sep 29, 2026 - Oct 1, 2026`, ISO ranges,
weekday-prefixed ranges) returning start/end + a provenance `form` label. It
never invents: a missing year/month, a reversed range, or an impossible date is
rejected as ambiguous rather than repaired. Wired into `normalize_candidate` as
a dedicated `date_range` field and as a fallback for a start value the plain
parser can't read (so a column mixing single dates and ranges just works).
Preview quality gained `range_count`, `range_parse_success_rate`,
`end_date_success_rate`, and `ambiguous_range_rejections`.

**Recurrence (`app/schemas/recurrence.py`, `app/extraction/recurrence.py`).** A
validated `RecurrenceSpec` (modes `parent_only` / `explicit_occurrences` /
`bounded_expand`) and a `dateutil`-backed expander. RRULE/RDATE/EXDATE,
explicit occurrence arrays, and detached/modified instances are supported;
RECURRENCE-ID matches a detached override to the slot it replaces; a cancelled
occurrence is flagged, never dropped. Bounded on every axis — future horizon,
per-parent cap, per-run budget, rule length, sub-daily refusal, wall-clock
guard. Occurrence identity is deterministic (source occurrence id → parent id +
RECURRENCE-ID → parent fingerprint + normalized start → bounded hash) and is
carried on `external_source_id` so a re-run matches an occurrence instead of
duplicating it, and so the shared parent UID never collapses the series to one
row. Times are wall-clock (DST-correct for a local listing). Config-driven via
`SiteConfiguration.recurrence` (default parent_only → existing behaviour
unchanged); expanded in the shared pipeline so preview quality reflects stored
events. A cancelled occurrence persists deactivated (`is_cancelled=True`,
`is_active=False`) — hidden, never deleted, and never silently reactivated.

**Geography (`app/schemas/geographic.py`, `app/services/geographic_filter.py`).**
A post-normalization, pre-persistence filter supporting locality/region/country,
postal codes/prefixes, address substrings, radius, bounding box, configured
aliases, and any/all mode. No fuzzy matching (word-boundary only) and no
geocoding (radius/bbox apply only when coordinates are already present).
Inclusion is decided from the event's own geography, never from its assigned
city. Missing-geography policy is reject / keep_with_warning / needs_review. The
decision is recorded once in the shared pipeline as provenance history; the
persistent run drops excluded events and flags needs_review ones. Preview
quality gained `geographic_considered/included/excluded/missing` and
`geographic_inclusion_rate`.

**Policy (Phase 8D integration).** `AutoOnboardingPolicy` gained
`require_date_range_parse_success` + `minimum_date_range_parse_success` and
`require_geographic_filter` + `minimum_geographic_inclusion_rate`; the decision
service's `_check_preview` gates on them; `PolicySnapshot`/`snapshot_policy` and
the policy admin form carry them. All default off/neutral, so installing 8G
changes no existing outcome. AI-origin approval stays denied by default.

**Migration `e3c9f21a7b48`** (revises `d7a1c4e83b60`): additive columns on
`events` (occurrence identity + cancellation, two indexes) and
`auto_onboarding_policies` (the four gates), every one server-defaulted;
self-contained, round-trips up/down on a scratch DB, non-destructive.

**Dependency added:** `python-dateutil>=2.9`.

**Verification:** new unit suites for the range parser (27), range
normalization + quality (6), the recurrence expander (15), the geographic filter
(10), the pipeline steps (6), plus policy-gate cases added to the qualification
suite; migration parity + configure + auto-onboarding suites pass. Ruff clean;
full suite: 958 passed.

**Commit:** `0d0fcba`.

## Phase 10 — durable scheduler and background processing

**Status:** complete.

Automatic refresh of approved, active sources from a **dedicated scheduler
process** (`python -m app.scheduler`), never one scheduler per web worker — the
web app starts no scheduler at all. APScheduler drives the ticks; the durable
source of truth is the database, so a crash/restart reconciles and resumes with
nothing lost or double-run.

**State (`app/models/scheduler.py`, migration `f4b2d90c1a57`).**
`scheduler_job_state` (one row per scheduled site) is the per-site lock
(running + holder + heartbeat), the durable schedule (next/last run, status), a
cancel request, a scheduler-level pause, and a structural-failure counter.
`scheduler_leader` is a single advisory row so exactly one process schedules
even if two are started.

**Service (`app/services/scheduler.py`).** Pure, unit-tested decision logic:
eligibility (ACTIVE + active + not archived + active city + valid approved
config + valid enabled schedule), stale-lock detection, next-run computation,
plus the DB ops — atomic per-site `try_acquire` (no overlap; a stale lock is
reclaimed), `release_lock`, `reclaim_stale_locks`, idempotent `reconcile`,
`pause_city_sites`, leader election/heartbeat, and a health summary.

**Runner (`app/services/scheduler_runner.py`).** Runs one extraction under the
lock with bounded exponential-backoff retries, heartbeats, cooperative
cancellation between attempts, structural-failure counting, and the
re-onboarding trigger — all injectable so tests never touch the network.

**Runtime (`app/scheduler/`).** `SchedulerRuntime` runs three ticks under
leadership: a heartbeat (reclaim stale locks + periodic reconcile), a dispatcher
(launch every due, eligible, not-running site with bounded concurrency), and an
onboarding-queue drain (the Phase 8C worker runs here, not per web worker).
`python -m app.scheduler` is the entry point with graceful SIGINT/SIGTERM
shutdown (in-flight runs finish).

**Post-failure re-onboarding (`app/services/scheduler_reonboarding.py`).** After
repeated structural failures, rerun detection, refresh the draft, preview it,
and notify reviewers with an approved-vs-detected comparison — the approved
configuration is **never** silently replaced; re-approval stays explicit and
audited.

**Lifecycle.** Leaving ACTIVE (deactivation/failing/archive) pauses the job
immediately (`transition_website` hook); city deactivation pauses all its sites
via eligibility + the periodic reconcile. Events and history are untouched.

**Admin controls (`app/routers/scheduler.py`).** `/admin/scheduler/health`,
run-now (schedules immediately; never runs inline in the web request), cancel,
pause, resume — all CSRF-protected (double-submit header) and permission-gated.

**Dependency added:** `APScheduler>=3.10,<4`. Startup commands + the whole
model documented in `docs/scheduler.md`.

**Verification:** 5 schedule-config, 10 service, 7 runner, 6 admin-endpoint
tests (33 new, minus one duplicate helper); migration round-trips on a scratch
DB and parity holds; website transition/approval suites unaffected. Ruff clean;
full suite: 985 passed.

**Commit:** `781677a`.

## Phase 11 — asynchronous geocoding

**Status:** complete.

A provider interface plus a Nominatim adapter, drained in the background from
the dedicated scheduler process. **Disabled by default** (Phase 8E pattern): no
live third-party request is ever made unless an administrator turns it on, and
tests inject a static provider.

**Providers (`app/services/geocoding/provider.py`).** `DisabledGeocoder` (the
default, reports unhealthy), `StaticGeocoder` (deterministic test double), and
`NominatimGeocoder` — descriptive User-Agent, >=1s minimum interval per
Nominatim policy, bounded timeout, retries, and a circuit breaker that trips
after repeated failures. A hit returns a `GeocodeResult`, a confident no-match
returns `None`, and a provider failure raises `ProviderUnavailable` — three
outcomes the service maps to completed / needs_review / failed.

**Service (`app/services/geocoding/service.py`).** Skip rules that never run:
events with source coordinates, events with a protected administrator
correction, and events with no usable address/venue (geo-filter-rejected events
are never persisted, so never reach here). Results are cached by
normalized-address hash (`geocode_cache`, positive and negative), written only
to the event's derived `geocoded_*` columns, and status advances through
pending -> completed / needs_review / failed / skipped. `retry_event_geocoding`
requeues (but refuses a still-protected skip); `drain_geocoding_queue` processes
a bounded batch and stops early if the provider goes unhealthy.

**Model (migration `a1c8e6f4920d`).** `events` gains `geocoded_latitude/longitude`,
`geocode_status`, `geocode_attempts`, `geocode_last_error`, `geocoded_at`; a new
`geocode_cache` table. `public_latitude/longitude` now prefer correction ->
source -> geocoded, so a geocoded coordinate shows only when there is nothing
better and never shadows a correction.

**Background + admin.** A geocoding drain tick runs in the scheduler process
(only when enabled). `/admin/geocoding` exposes a status breakdown and a
CSRF-protected, permission-gated per-event retry.

**Config:** geocoding settings in `config.py` (disabled, no key needed). No new
dependency (uses the existing `httpx`).

**Verification:** 7 provider/helper, 9 service, 4 admin-endpoint tests (20 new);
migration round-trips on a scratch DB and parity holds; public-events suite
unaffected by the coordinate-preference change. Ruff clean; full suite: 1005
passed.

**Commit:** `32665b2`.

## Phase 12 — complete public event experience

**Status:** complete.

Extends the existing polished, responsive homepage into the full public
experience without disturbing its styling.

**Filters + shareable URLs (`app/routers/home.py`, `repositories/public_events.py`).**
A single `_Filters` parser drives the list route, the city route, and the map
endpoint identically: text search (title/venue/description), city, category,
source, one-time/recurring, explicit date range, and `Today` / `This weekend`
presets (a preset serializes as `?preset=...` so the URL stays tidy and
self-contained). Every control is a GET field and every link carries the
current query string, so any view is shareable.

**City picker + city pages.** `/city/{slug}` pins a city and reuses the same
rendering; the homepage shows a city picker when no city is selected.

**Map (`/events/map`, `static/js/event-map.js`).** A `view=list|map` toggle;
the map is progressive enhancement over the always-rendered list (the
accessible non-map equivalent). Leaflet + MarkerCluster (CDN) plot OpenStreetMap
tiles with clustering and keyboard-accessible popups built via the DOM (never
string-interpolated HTML). The `/events/map` JSON carries only visible, matching
events that have usable coordinates and only safe fields — no provenance, raw
records, configuration, or correction history. Tile URL/attribution and a
fallback image are configurable.

**Occurrence-aware.** `_base_public_query` excludes recurrence parents, so a
series never renders a parent card alongside its occurrence cards; only
concrete occurrences and single events appear. Public coordinates follow the
correction -> source -> geocoded preference.

**Accessibility.** Search landmark, labeled controls, heading hierarchy, empty
alt on decorative fallbacks, `aria-current` on the active view, and the list as
the full non-map equivalent.

**No migration, no new dependency** (Leaflet is loaded from CDN only in map
view; tests never execute it).

**Verification:** 11 new tests (search, source, recurrence, presets, city page,
recurrence-parent exclusion, map coords-only, map payload has no sensitive
fields, geocoded-coordinate preference, map container render); existing
home/public suites unaffected. Ruff clean; full suite: 1016 passed.

**Commit:** `6d45344`.

## Phase 13 — saved events, followed cities, and alerts

**Status:** complete.

Registered-user engagement, entirely per-user and privacy-scoped — no route
reads or writes another user's data, and registered users retain zero
administrative permissions.

**Model (migration `b7e2f5a4c318`).** `saved_events`, `user_follows` (a
polymorphic city/category/source follow), `alert_preferences` (channels,
frequency, per-type toggles, an unsubscribe token), and `alert_deliveries` (a
ledger whose unique `(user, alert_key, channel)` is what prevents duplicate
alerts across re-scrapes and retried digests).

**Services.** `engagement` (save/unsave, follow/unfollow, followers,
preferences, token unsubscribe); `email` (a pluggable `EmailSender` — default
`NoopEmailSender` sends nothing, plus console/memory backends — so no real
email is ever sent unless enabled AND a backend is configured); `alerts`
(generation + delivery). In-app alerts reuse the notification system with a
per-user fingerprint; a follower with no stored preferences gets sensible
defaults (in-app on, email off).

**Alerts covered.** New events in a followed city/category/source; cancellation
and update alerts to users who saved the event; day-before reminders for saved
events. Immediate email sends now; daily/weekly queue as pending and are
batched into one digest per period. Everything deduped by the ledger.

**Wiring.** `run_extraction` fires new-event/update/cancellation alerts on
persist (best-effort, isolated — an alert failure never affects the run). The
scheduler process runs an alerts tick (reminders + digests).

**UI.** Save/unsave and follow-city buttons on the event detail page, a saved-
events page, an alert-preferences page, and a token-based public unsubscribe
page (email only; in-app untouched). All state-changing posts are CSRF-protected
and PRG-redirected.

**Config:** email settings (disabled, noop backend). No new dependency.

**Verification:** 6 engagement, 6 alerts, 7 endpoint tests (19 new) covering
idempotency, privacy between users, dedup, preference-off, digest batching,
cancellation/reminder alerts, CSRF, and login requirements; migration
round-trips on a scratch DB; extraction suite unaffected by the alert hook.
Ruff clean; full suite: 1034 passed.

**Commit:** `98e6aa7`.

## Phase 14 — OAuth and external identities

**Status:** complete.

Provider-independent external authentication built on **Authlib** (the protocol
is never hand-rolled). Google, Microsoft, and Facebook are each configured
independently and are enabled only when they have a client id AND secret, so
the app starts with any or all disabled and never offers a provider without
credentials. No real OAuth app is needed — tests use a mocked provider.

**Model (migration `c9d4a1b6e072`).** `external_identities` links a local user
to a provider account, unique on `(provider, subject)` — the duplicate-identity
guard. It stores no third-party password and retains no provider tokens, only
the subject, an optionally-verified email, and display fields.
`oauth_login_states` holds the short-lived, one-time CSRF `state` + OIDC
`nonce` server-side, so validation needs no session middleware.

**Providers (`app/services/oauth/`).** A `ProviderSpec` per provider +
`AuthlibProvider` (authorize-URL build, code→token exchange, userinfo fetch via
Authlib) + `MockProvider` for tests. `is_provider_enabled` /
`enabled_provider_names` gate on credentials.

**Login (`app/services/oauth_login.py`).** `start_login` mints and stores state
+ nonce and returns the authorize URL; `complete_login` validates state
(one-time, expiring, provider-matched), fetches the identity, and resolves the
user with every required rule: disabled-user rejection, duplicate-identity
protection, **safe account linking only on a provider-verified email**,
unverified-email conflict refusal (never hijacks an existing account),
missing-email rejection, and a local-only redirect allowlist. Session-fixation
prevention is inherent — the callback mints a brand-new session token.

**Routes (`app/routers/oauth.py`).** `/auth/oauth/{provider}` (start) and
`/auth/oauth/{provider}/callback`; a disabled provider 404s; success mints a
session, sets `last_login_at`, and audits; a failure is audited and returns to
the login page. Login page shows "Continue with …" for enabled providers.

**Config:** per-provider client id/secret (all empty by default) + redirect
base. **Dependency added:** `Authlib>=1.3`.

**Verification:** 10 service + 5 endpoint tests (15 new) — state validation
(invalid/expired/one-time), new-user creation, verified-email linking,
unverified-email conflict, returning-identity, disabled-user rejection,
missing-email, provider enable/disable, disabled-provider 404, full flow mints a
session, bad-state/missing-code fail safely; migration round-trips on a scratch
DB; login suite unaffected. Ruff clean; full suite: 1049 passed.

**Commit:** `cadc3f0`.

## Phase 15 — operational dashboard and reporting

**Status:** complete.

One read-only admin view (`GET /admin/reports`, gated on `reports.view`)
aggregating system health.

**Service (`app/services/reporting.py`).** `build_operational_report` computes,
with bounded queries: sources by onboarding status; the onboarding queue;
geocoding statuses; top-line counts (active/failing/needs-review sources,
failed/blocked runs, validation errors, unsupported reports, drift proposals,
geocoding failures, duplicate queue); recurrence truncations and geography
exclusions (scanned from a bounded window of recent run warnings); scheduler
health; AI usage; and short recent lists of runs, validation errors,
automatic-onboarding decisions, the duplicate queue, notifications, unsupported
reports, and a paginated audit log.

**Redaction.** Only counts and safe fields are surfaced — never secrets, API
keys, cookies, auth headers, OAuth tokens, provider credentials, raw source
content, audit before/after payloads, or other users' preferences. (A test
seeds a secret-bearing audit row and asserts nothing sensitive appears.)

**UI.** A metric-card grid, scheduler/AI health, status breakdowns, recent-
activity tables with genuine empty states, and audit-log pagination.

**No migration, no new dependency.**

**Verification:** 6 tests (empty state, seeded counts incl. recurrence/geo
markers, audit-payload redaction, AI-usage carries no key, permission gating,
admin render). Ruff clean; full suite: 1055 passed.

**Commit:** `a3d4529`.

## Phase 16 — legacy comparison and source migration

**Status:** complete.

A comparison workflow that pits the new engine's preview against a legacy
source's events. `legacy_app` stays strictly read-only: the legacy events
database is opened `mode=ro`, no legacy scraper is run, no legacy row is written,
and no scheduler is started.

**Engine (`app/services/legacy_comparison.py`).** `compare_events` is pure and
pattern-independent: given normalized `ComparableEvent`s from each side it
reports matched pairs (with per-field differences over title/date/end/time/
venue/address/url/category/image/recurrence), legacy-only, new-only, likely
duplicates (repeated match keys), and validation differences (new candidates
that would not persist). `read_legacy_events` reads one source's events from
`legacy_app/events.db` read-only and raises `LegacyUnavailable` (recorded, never
fatal) when it cannot. `run_comparison` ties the new preview to the legacy read.

**Migration status (migration `d5f1a3c8b940`).** `websites` gains
`legacy_migration_status` (pending | migrated | unavailable), `legacy_source_name`,
and `legacy_migrated_at`; `set_migration_status` records the review outcome
without ever touching live extraction. A legacy source that no longer reads is
marked unavailable rather than blocking the phase.

**Admin (`app/routers/legacy_comparison.py`).** A comparison view (runs the new
preview + reads legacy, shows the full diff with empty states) and a
CSRF-protected status control.

**Verification:** 11 tests — matched/legacy-only/new-only, field differences,
likely duplicates, validation differences, recurrence difference, read-only
reader returns real data, the reader's connection cannot write (INSERT raises
on the `mode=ro` handle), missing DB is unavailable, candidate mapping, status
persistence, and the status endpoint; migration round-trips on a scratch DB.
Ruff clean; full suite: 1066 passed.

**Commit:** `5ccff88`.

## Phase 17 — optional AI event enrichment

**Status:** complete.

Advisory-only AI enrichment reusing the Phase 8E disabled-by-default posture
and its budget + circuit-breaker tracker. AI may suggest a category (for
uncategorized events), tags, a short summary, an audience, a family-friendly
flag, duplicate candidates, and extraction-error summaries. It may NOT invent
dates/times/venues/addresses/URLs, approve/activate/publish/delete, change
permissions, override validation, or run code — the service only ever writes to
`event_enrichments`, never an event's authoritative fields.

**Schema + model (migration `e8b3f2c05a19`).** `EnrichmentSuggestion` is a
closed, bounded structured-output model with no field for any prohibited value.
`event_enrichments` stores the suggestion, cached by `(event_id,
prompt_version, input_hash)`.

**Service (`app/services/ai_enrichment.py`).** `enrich_event` runs only on
unresolved (uncategorized) events, sends a minimized public-fields-only input,
gates on the shared budget/circuit breaker, validates the structured output,
drops a category suggestion not in the active taxonomy, caches by input hash +
prompt version, and returns None (never raises) when disabled/over-budget/
failed — so an AI failure can never fail extraction or public display.
`summarize_extraction_errors` covers the error-summary task; `mark_enrichment`
records a human's applied/rejected decision. The default provider is disabled;
tests inject an echo/mock provider.

**Verification:** 10 tests — disabled produces nothing, echo suggestion stored,
result cached (a later failing provider still returns the cache), hallucinated
category dropped by taxonomy, resolved events never sent, authoritative fields
never touched, AI failure returns None and trips the circuit, error summary,
tag bounding, status marking; migration round-trips on a scratch DB. Ruff
clean; full suite: 1076 passed.

**Commit:** `98baeb8`.

## Phase 18 — production hardening and deployment preparation

**Status:** complete. **This completes the master implementation plan
(Phases 8B–18).**

Prepares for production without performing any deployment (no hosting, cloud
accounts, DNS, certificates, live OAuth apps, or exposed secrets).

**Config + readiness (`app/core/production_checks.py`).** Production-hardening
settings (trusted hosts, security headers, HTTPS assumption, request-size cap,
rate-limit backend, Redis URL). `production_blockers`/`is_production_ready` turn
"not ready while it depends on SQLite / in-process rate limiting / dev cookies /
no HTTPS / no trusted hosts" into a concrete checklist, surfaced at
`/health/ready`.

**Security (`app/core/middleware.py`).** `SecurityHeadersMiddleware` (CSP,
X-Frame-Options DENY, nosniff, referrer policy, HSTS behind HTTPS),
`MaxBodySizeMiddleware` (413 on oversized bodies), and `TrustedHostMiddleware`
when hosts are configured. Existing CSRF, SSRF, session, and secret-reference
protections documented in `docs/security.md`.

**Observability.** `/health`, `/health/live`, and `/health/ready` (DB
connectivity, scheduler leader freshness, AI provider health, production
blockers); structured logging + correlation IDs (existing); the operational
dashboard from Phase 15; safe metrics derived from both.

**Artifacts.** `Dockerfile` (one image, role by command), `docker-compose.prod.yml`
(web / single scheduler / Postgres / Redis / one-shot migrate), `.env.example`
(annotated), `.github/workflows/ci.yml` (Ruff + migration round-trip + full
suite), and a disabled deploy-workflow template.

**Docs.** `architecture.md`, `operations.md`, `deployment.md`, `security.md`,
`automatic-onboarding.md`, `extraction-patterns.md`, `recovery.md` — covering
env vars, migrations/backup/restore/rollback, process split + single-scheduler
ownership, Playwright/OAuth/AI/geocoder setup, retention (event/audit/evidence),
secret rotation, incident recovery, and compliance (robots.txt, source terms,
Nominatim policy, provider terms, privacy, deletion, retention).

**Verification:** 5 tests (dev defaults not production-ready, hardened settings
ready, security headers present, oversized request rejected, liveness +
readiness). Ruff clean; full suite: 1081 passed.

**Commit:** `8e88706`.
