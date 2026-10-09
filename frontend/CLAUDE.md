# Frontend guide

Server-rendered HTML: Jinja2 templates plus plain CSS and JavaScript. The
backend renders these templates and serves `static/` at `/static`, so there is
no build step and no package manager here. Read the root `CLAUDE.md` first.

## Layout

| Path | Role |
|---|---|
| `templates/base.html` | Public page shell; public pages `{% extends "base.html" %}` |
| `templates/admin_base.html` | Admin shell; admin pages live in `templates/admin/` |
| `templates/_*.html`, `templates/admin/_macros.html` | Shared partials and macros |
| `static/style.css` | All styles |
| `static/js/` | Small scripts, one per behaviour (city search, map, admin forms, ...) |

## Rules

- **No inline JavaScript.** The Content-Security-Policy is `script-src 'self'`, so
  inline `<script>` blocks, `onclick=`/`onsubmit=` attributes and `javascript:`
  URLs silently do nothing. Put code in `static/js/` and load it with
  `<script src="/static/js/...">`. `backend/tests/test_csp_no_inline_scripts.py`
  enforces this. JSON-LD (`type="application/ld+json"`) blocks are fine.
- Every `<form method="post">` includes
  `<input type="hidden" name="csrf_token" value="{{ csrf_token }}" />`.
- Keep pages accessible: real `<label>`s for inputs, `alt` text on images,
  semantic headings, visible focus styles.
- Public event and town pages carry schema.org JSON-LD and canonical/Open Graph
  tags for search engines and AI tools; keep them when editing those pages.
- Template variables come from the routers in `backend/app/routers/`; filters and
  globals (`human_date`, `absolute_url`, ...) are registered in
  `backend/app/core/templating.py`.

## Checking changes

From `backend/`: `python -m pytest -q` (template tests include
`test_public_ui.py`, `test_admin_nav.py`, `test_csp_no_inline_scripts.py`), then
run the app (`uvicorn app.main:app --reload --port 8100`) and look at the page.
