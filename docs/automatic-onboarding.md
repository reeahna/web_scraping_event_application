# Automatic onboarding

Onboarding takes a source URL to a running, approved extraction configuration.
Every step is deterministic and auditable; full automation is policy-gated and
conservative by default.

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

## Re-onboarding after failure

Repeated structural failures re-run detection and refresh the draft + preview,
then notify reviewers. The approved configuration is never silently replaced.

## Provenance & audit

Every decision records a compact, reproducible snapshot of the metrics and
thresholds actually consulted. System actions are audited distinctly from human
actions (`actor_type`). AI-origin configurations remain denied by default.

## Onboarding a whole town with Claude

Imports -> **Onboard a town** takes a college town's name (and optionally its
state and universities) and does what the daily manual routine did: find the
town's event listing pages, write the city record and the source CSV, create
the city and queue the sources.

1. The admin page saves a `town_onboarding_requests` row. The scheduler
   process picks up one queued town per tick (`_town_onboarding_tick`), so the
   few minutes of searching never happen inside a web request.
2. `app.services.town_onboarding.discover_town` asks Claude
   (`TOWN_ONBOARDING_MODEL`, with web search and web fetch) for at least
   `TOWN_ONBOARDING_MIN_SOURCES` listing pages (default 8), with at least
   `TOWN_ONBOARDING_MIN_PER_SCOPE` (default 3) on campus and 3 in town. Claude
   opens each page to confirm it lists upcoming events, then answers through a
   strict `submit_town` tool.
3. `check_proposal` validates the answer: IANA timezone, two-letter state,
   public http(s) URLs only, no social media, ticketing or nationwide
   aggregator pages, no duplicates. Bad pages are dropped; if too few remain,
   the problems go back to Claude and it searches again (up to three times).
4. `apply_proposal` creates the city (slug `<town>-<state>`, e.g.
   `ann-arbor-mi`; an existing city with that slug or name is used unchanged)
   and queues the sources through `create_batch_from_submission`, with
   `source_kind = "claude"`. From there it is the normal pipeline above:
   detection, preview, policy, review. Nothing is approved or activated here.

The request page shows the sources by campus and town, the pages that were
left out and why, token and search usage, and a CSV download in the same
format as the daily files. `scripts/onboard_town.py` runs the same search from
the command line (`--csv` to save the file, `--apply` to create the town).

Without `ANTHROPIC_API_KEY` the page says so and nothing calls the API.
