# Bulletin — guide for AI coding tools

Bulletin is a college-town events site. It scrapes event listings from local
sources on a schedule and shows them town by town, with an admin UI for
managing towns, sources and events.

## Layout

| Folder | What's in it |
|---|---|
| `backend/` | The Python app (FastAPI, SQLAlchemy, Alembic): routes, models, scraping engine, scheduler, tests. See `backend/CLAUDE.md`. |
| `frontend/` | Jinja2 templates and static CSS/JS. Rendered and served by the backend; there is no separate build step. See `frontend/CLAUDE.md`. |
| `docs/` | Architecture, extraction patterns, onboarding, scheduler, security, operations, deploying to Render. |
| `render.yaml` | Render Blueprint: one Docker service built from the repo root with `backend/Dockerfile`. |
| `.github/workflows/ci.yml` | Ruff, migration round trip, and the test suite on SQLite and PostgreSQL. |

The backend finds the frontend through `backend/app/paths.py` (`frontend/` is a
sibling of `backend/`, locally and in the Docker image).

## Everyday commands

All run from `backend/` with its venv active:

```bash
pip install -e ".[dev]" && python -m playwright install chromium   # first time
python -m alembic upgrade head          # create/upgrade the local SQLite DB
uvicorn app.main:app --reload --port 8100
python -m app.scheduler                  # background scraping, separate process
python -m ruff check .                   # lint (must pass in CI)
python -m pytest -q                      # full suite, a few minutes
python -m pytest tests/test_public_ui.py -q   # one file
```

## Rules that apply everywhere

- Run `ruff check .` and the relevant tests before calling a change done.
  Template changes are covered by backend tests too (`tests/test_public_ui.py`,
  `tests/test_csp_no_inline_scripts.py`, ...).
- Changing a model means adding an Alembic migration and checking
  `upgrade head`, `downgrade -1`, `upgrade head`.
- Never commit secrets or `.env`; configuration is environment variables
  (`backend/.env.example` lists them, `backend/README.md` explains them).
- There are no passwords: sign-in is Google/Microsoft/Facebook OAuth only.
- More detail: `backend/README.md` (features and configuration) and `docs/`.
