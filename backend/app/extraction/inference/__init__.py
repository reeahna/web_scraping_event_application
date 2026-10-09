"""Deterministic configuration inference.

Turns one already-fetched listing response plus its detection result into a
complete, validated `SiteConfiguration` — no LLM, no browser, no per-site
Python, no hostname/site-name conditional anywhere in this package.

Every module here is pure data-in/data-out (no Session, no network I/O) so
inference is unit-testable against fixtures exactly like the rest of
app.extraction. The one orchestration step that *does* need a Session and a
fetch — detect -> propose -> save draft -> preview -> score -> classify —
lives in app.services.auto_onboarding.
"""
