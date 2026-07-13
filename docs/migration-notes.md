# Migration Notes — Phase 0: Repository Reorganization

Date: 2026-07-13

## What happened

The existing (working) city events aggregator was relocated from the repository root into `legacy_app/`, in preparation for building a replacement application inside `new_app/`. This was a pure file-move — **no application code was refactored, no imports were rewritten, no modules were renamed, no database schema changed, and no scraper or filtering logic changed.**

## Files moved (git mv — history preserved)

- `main.py` → `legacy_app/main.py`
- `database.py` → `legacy_app/database.py`
- `scraper_runner.py` → `legacy_app/scraper_runner.py`
- `requirements.txt` → `legacy_app/requirements.txt`
- `scrapers/` → `legacy_app/scrapers/` (all subfolders and files)
- `templates/` → `legacy_app/templates/`
- `static/` → `legacy_app/static/`

## Files moved (plain move — untracked/gitignored, no git history to preserve)

- `geocoder.py` → `legacy_app/geocoder.py`
- `events.db` → `legacy_app/events.db`
- `events.csv` → `legacy_app/events.csv`

## Files left at repository root

- `.git/`, `.gitignore` (updated), `README.md` (rewritten as a top-level pointer)
- `.claude/` (Claude Code tooling config — not application-specific)
- `venv/` — the existing virtualenv was **intentionally left at the repo root, untouched**, so the current environment kept working through relocation and verification. It is a disposable, machine-specific artifact, not application code. It is self-ignored via its own internal `venv/.gitignore`. A fresh virtualenv should eventually be created inside `legacy_app/` (and separately inside `new_app/` once that app exists) — this was deferred as a deliberate choice, not an oversight.

## Why no code changes were needed

The legacy app only ever used paths relative to the process's **current working directory** (CWD), not to `__file__`:

- `database.py`: `DB_PATH = os.getenv("DB_PATH", "events.db")`
- `main.py`: `Jinja2Templates(directory="templates")`, `StaticFiles(directory="static")`
- `database.py` → `export_csv(path="events.csv")` default, called from `scraper_runner.py`
- All internal imports (`from database import ...`, `from scrapers.bloomington_in.iu_events import ...`) resolve via the CWD being on `sys.path`.

Since none of these are anchored to `__file__`, the **only requirement to keep the legacy app working unmodified is launching it with `legacy_app/` as the working directory.** This is a documented operational change, not a code change.

## New legacy startup command

```bash
cd legacy_app
uvicorn main:app --reload --port 8000
```

(Previously: `uvicorn main:app --reload --port 8000` from the repo root.)

## Rollback instructions

All moves were done with `git mv` where the file was tracked, so history and renames are visible in `git status` / `git diff --staged`. To roll back before committing:

```bash
# Undo all staged moves and restore original working-tree layout
git restore --staged .
# Then move files back (git mv in reverse), e.g.:
git mv legacy_app/main.py main.py
git mv legacy_app/database.py database.py
git mv legacy_app/scraper_runner.py scraper_runner.py
git mv legacy_app/requirements.txt requirements.txt
git mv legacy_app/scrapers scrapers
git mv legacy_app/templates templates
git mv legacy_app/static static
mv legacy_app/geocoder.py geocoder.py
mv legacy_app/events.db events.db
mv legacy_app/events.csv events.csv
rmdir legacy_app new_app docs   # once empty
```

Nothing was committed automatically as part of this reorganization — the changes exist only in the working tree/staging area until explicitly committed.

## Verification performed

See the end-of-phase report in the conversation for full verification results (server startup, template/static loading, database, scraper imports, APScheduler behavior, and smoke test).

## Remaining risks

- No automated tests existed before this phase; a single smoke test was added (see report) but it does not cover scraper network behavior, geocoding, or full page rendering.
- `venv/` at the repo root still has import-path assumptions baked into some activation scripts (absolute paths); it was not touched, but should eventually be recreated per-app.
- If any future tooling or scripts (outside this repo, e.g. deployment configs, cron jobs) reference the old root-level paths (`uvicorn main:app` from repo root, or a hardcoded `events.db` path), they will need to be updated to point at `legacy_app/`.
