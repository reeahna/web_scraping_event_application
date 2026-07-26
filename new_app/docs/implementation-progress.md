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

**Commit:** `feat: add policy-controlled automatic onboarding` (hash recorded with the next
phase update).
