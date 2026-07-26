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

**Commit:** (recorded with the next phase update).
