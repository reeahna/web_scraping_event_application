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
see below.

**Commit:** (recorded with the next update).
