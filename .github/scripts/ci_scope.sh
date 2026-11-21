#!/usr/bin/env bash
# Decides which CI jobs a change needs. Used by the `scope` job in ci.yml.
#
# Two modes:
#   ci_scope.sh classify      reads changed paths (one per line) on stdin and
#                             prints test=, postgres=, browsers= lines. Pure,
#                             no network, so it can be tested locally.
#   ci_scope.sh               (no argument) works out the changed paths for the
#                             current GitHub event, classifies them, and on a
#                             push to main checks whether the merged PR already
#                             passed CI on exactly this code. Writes the result
#                             to $GITHUB_OUTPUT.
#
# Rule of thumb: when anything is unclear (unknown event, missing history, an
# API error, an unrecognised file), run more, never less.

set -uo pipefail

# Files whose change can affect every job: CI itself, dependencies, the test
# harness and build files. Any of these runs everything.
FULL_RE='^(\.github/|backend/pyproject\.toml$|backend/requirements[^/]*\.txt$|backend/Dockerfile$|backend/alembic\.ini$|backend/tests/conftest\.py$)'

# Documentation nothing reads at run time. A change made only of these runs no
# jobs. CHANGELOG.md is NOT documentation here: the app renders it on
# /whats-new and /admin/version-history and the release script parses it.
DOCS_RE='^(docs/.*|.*\.md)$'

# Files that change what a browser renders: templates, CSS and JS, and the
# backend code that builds pages, sets headers (CSP, cookies) or formats what
# pages show. A change here runs the Chromium, Firefox and WebKit layout jobs.
# Chromium layout checks also run inside the `test` job on every code change,
# so a backend change outside this list is still checked in a real browser.
UI_RE='^(frontend/|CHANGELOG\.md$|backend/app/routers/|backend/app/main\.py$|backend/app/paths\.py$|backend/app/dependencies\.py$|backend/app/schemas/|backend/app/core/(templating|formatting|middleware|security|csrf|flash|permissions)\.py$|backend/tests/test_responsive_layout\.py$)'

# Files that cannot change database behaviour: templates and static files, and
# the changelog. A change made only of these (plus docs) skips the PostgreSQL
# job. Anything else, including backend/data/ seed CSVs (PostgreSQL enforces
# column lengths that SQLite ignores), runs it.
NO_DB_RE='^(frontend/|CHANGELOG\.md$)'

classify() {
  local test=false postgres=false browsers=false any=false path
  while IFS= read -r path || [ -n "$path" ]; do
    [ -z "$path" ] && continue
    any=true
    if [[ "$path" =~ $FULL_RE ]]; then
      test=true; postgres=true; browsers=true
      continue
    fi
    if [[ "$path" =~ ^CHANGELOG\.md$ ]] || ! [[ "$path" =~ $DOCS_RE ]]; then
      test=true
      [[ "$path" =~ $NO_DB_RE ]] || postgres=true
      [[ "$path" =~ $UI_RE ]] && browsers=true
    fi
  done
  # No changed files listed at all is suspicious (for example a lookup that
  # silently failed): run everything.
  if [ "$any" = false ]; then
    test=true; postgres=true; browsers=true
  fi
  echo "test=$test"
  echo "postgres=$postgres"
  echo "browsers=$browsers"
}

if [ "${1:-}" = "classify" ]; then
  classify
  exit 0
fi

out="${GITHUB_OUTPUT:-/dev/stdout}"
emit_all() {
  echo "reason=$1"
  { echo "test=true"; echo "postgres=true"; echo "browsers=true"; echo "verified_merge=false"; } >> "$out"
  exit 0
}

zero='0000000000000000000000000000000000000000'
case "${EVENT_NAME:-}" in
  pull_request)
    base="${PR_BASE_SHA:-}"; head="${PR_HEAD_SHA:-}"
    [ -n "$base" ] && [ -n "$head" ] || emit_all "missing PR shas"
    # Three dots: only what this PR changes, from where it left main.
    files=$(git diff --name-only "$base...$head") || emit_all "git diff failed"
    ;;
  push)
    before="${PUSH_BEFORE:-}"; after="${PUSH_AFTER:-}"
    { [ -n "$before" ] && [ "$before" != "$zero" ]; } || emit_all "no previous commit"
    files=$(git diff --name-only "$before" "$after") || emit_all "git diff failed"
    ;;
  *)
    emit_all "event ${EVENT_NAME:-unknown} runs everything"
    ;;
esac

echo "Changed files:"
printf '%s\n' "$files" | sed 's/^/  /'
result=$(printf '%s\n' "$files" | classify)

verified=false
if [ "${EVENT_NAME}" = "push" ]; then
  # Was this push the merge of a PR whose CI already passed on exactly the code
  # now on main? That holds when the PR's head already contained the previous
  # main commit (the branch was up to date, so the merge adds nothing the PR
  # run did not test) and a CI run for that head finished successfully.
  # Every lookup failure leaves verified=false, which runs the full set.
  pr_json=$(gh api "repos/$REPO/commits/$after/pulls" \
    --jq "[.[] | select(.merged_at != null and .merge_commit_sha == \"$after\" and .base.ref == \"main\")][0] // empty" 2>/dev/null) || pr_json=""
  if [ -n "$pr_json" ]; then
    pr_number=$(printf '%s' "$pr_json" | jq -r '.number')
    pr_head=$(printf '%s' "$pr_json" | jq -r '.head.sha')
    echo "Merged PR #$pr_number, head $pr_head"
    git fetch --quiet --filter=blob:none origin "$pr_head" 2>/dev/null || true
    if git merge-base --is-ancestor "$before" "$pr_head" 2>/dev/null; then
      ok_runs=$(gh api "repos/$REPO/actions/workflows/ci.yml/runs?head_sha=$pr_head&event=pull_request&status=success&per_page=1" \
        --jq '.total_count' 2>/dev/null) || ok_runs=0
      if [ "${ok_runs:-0}" -gt 0 ] 2>/dev/null; then
        verified=true
        echo "PR #$pr_number was up to date with main and its CI passed on this exact code."
      else
        echo "No successful CI run found for the PR head: running the full set."
      fi
    else
      echo "PR #$pr_number was behind main when merged, so main now holds a combination its CI never ran: running the full set."
    fi
  else
    echo "Not a PR merge (or lookup failed): running the full set."
  fi
fi

if [ "$verified" = true ]; then
  # Same code already passed PostgreSQL and all browsers on the PR. Keep the
  # `test` job on main (lint, dependency audit, migrations, full SQLite suite
  # with the Chromium layout check) to catch new advisories or dependency
  # releases since the PR ran.
  result=$(printf '%s\n' "$result" | sed -e 's/^postgres=.*/postgres=false/' -e 's/^browsers=.*/browsers=false/')
else
  # A merge that was not verified runs everything that the merged files need,
  # and always PostgreSQL, because two PRs that each passed alone can still
  # conflict in the database layer.
  if printf '%s\n' "$result" | grep -q '^test=true' && [ "${EVENT_NAME}" = "push" ]; then
    result=$(printf '%s\n' "$result" | sed -e 's/^postgres=.*/postgres=true/')
  fi
fi

echo "Decision:"
printf '%s\n' "$result" "verified_merge=$verified" | sed 's/^/  /'
printf '%s\n' "$result" "verified_merge=$verified" >> "$out"
