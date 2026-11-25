# Automatic onboarding

Onboarding takes a source URL to a running, approved extraction configuration.
Every step is deterministic and auditable; full automation is policy-gated and
conservative by default.

For the step-by-step process of adding a new college town (research, approval
packages, deployment gate, schedules and first-run checks), see
`docs/town-onboarding.md`.

## Pipeline

1. **Detect** — fetch the listing and run `PatternRegistry` detection (no
   hostname/site-name conditionals; dispatch is always registry-based).
2. **Propose** — deterministically infer a `SiteConfiguration` (selectors,
   json paths, date formats, pagination, fetch settings). No executable
   content is ever produced.
3. **Draft** — save the proposed configuration as the editable draft, tagged
   with its origin (deterministic-structured, deterministic-generic-html,
   ai-suggested, administrator-manual, imported).
4. **Preview** — run the engine against the draft **without persisting**;
   score quality (valid %, coverage, date-parse success, duplicate rate,
   date-range success, geographic inclusion, warnings).
5. **Policy decision** — `AutoOnboardingDecisionService.evaluate` compares the
   preview and configuration against the applicable policy and records an
   append-only decision.
6. **Approve / activate** — only if the policy permits; otherwise the source
   waits for manual review. Approval freezes a snapshot; activation makes it
   eligible for scheduling.

## Policies

`AutoOnboardingPolicy` is fully normalized (every threshold is a column) and
resolved by precedence: batch-selected → city → global default. The seeded
default has automatic approval **and** activation **off**, generic_html_cards
off, and browser-required and AI-origin configurations **denied** — so
installing the feature changes no existing source's outcome.

Enabling automatic approval/activation requires typing an explicit confirmation
phrase. Policies gate on detector confidence, event counts, valid/rejected
percentages, coverage, date-parse and date-range success, geographic-filter
quality, warning counts, and stricter bars for generic HTML.

## Bulk onboarding

A persisted queue (`onboarding_jobs`) accepts many URLs; the scheduler process
drains it. Existing-website matching prefers non-archived matches; duplicates
are prevented.

Each job first fetches the page once (the pre-flight fetch). If that fetch is
refused by edge protection or answers with an HTTP error or a connection
failure, the source still gets a Website so it shows in Admin > Websites: a
blocked source is moved to Blocked, any other failure stays in Draft. It stays
inactive and unscheduled and is never configured, approved or activated. The
job keeps the exact reason (for example `HTTP 405` or
`edge_protection:http_403`), and the Website page shows it with a link to the
job. Submitting or retrying the same URL later reuses that Website rather than
creating another. A URL refused by the SSRF check (a private address or a host
that does not resolve) is not a source, so no Website is created for it.

## Re-onboarding after failure

Repeated structural failures re-run detection and refresh the draft + preview,
then notify reviewers. The approved configuration is never silently replaced.

## Provenance & audit

Every decision records a compact, reproducible snapshot of the metrics and
thresholds actually consulted. System actions are audited distinctly from human
actions (`actor_type`). AI-origin configurations remain denied by default.
