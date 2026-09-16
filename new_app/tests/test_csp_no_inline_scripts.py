"""No template may use inline JavaScript.

The app sets `script-src 'self'` with no 'unsafe-inline'
(app.config.Settings.content_security_policy), so the browser refuses both
inline <script> blocks and inline handler attributes. Code written that way
does not fail loudly, it simply never runs, which is how fifteen
`onsubmit="return confirm(...)"` guards on destructive admin actions came to be
silently doing nothing: those forms submitted with no prompt at all.

Behaviour belongs in app/static/js/, loaded with <script src>.

Each check walks every template and reports all offenders at once rather than
parametrising, which keeps this a fast file scan instead of 150+ test cases.
"""

import re
from pathlib import Path

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "app" / "templates"

INLINE_HANDLER = re.compile(r"""\son[a-z]+\s*=\s*["']""", re.I)
INLINE_SCRIPT = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>", re.I)
JS_URL = re.compile(r"""(?:href|action)\s*=\s*["']\s*javascript:""", re.I)


def _templates():
    found = sorted(TEMPLATES_DIR.rglob("*.html"))
    assert found, f"no templates under {TEMPLATES_DIR}"
    return found


def _offenders(pattern: re.Pattern) -> list[str]:
    hits = []
    for template in _templates():
        # {# ... #} blocks discuss these patterns; they never reach the browser.
        body = re.sub(r"\{#.*?#\}", "", template.read_text(encoding="utf-8"), flags=re.S)
        for match in pattern.finditer(body):
            line = body.count("\n", 0, match.start()) + 1
            excerpt = body[match.start() : match.start() + 40].strip()
            hits.append(f"{template.relative_to(TEMPLATES_DIR)}:{line}: {excerpt!r}")
    return hits


def test_no_inline_event_handlers():
    offenders = _offenders(INLINE_HANDLER)
    assert not offenders, (
        "Inline handlers are blocked by the CSP and never run. Use a data- "
        "attribute handled by app/static/js/admin-forms.js:\n  " + "\n  ".join(offenders)
    )


def test_no_inline_script_blocks():
    offenders = _offenders(INLINE_SCRIPT)
    assert not offenders, (
        "Inline <script> is blocked by the CSP. Move the code to app/static/js/ "
        "and load it with <script src>:\n  " + "\n  ".join(offenders)
    )


def test_no_javascript_urls():
    offenders = _offenders(JS_URL)
    assert not offenders, "javascript: URLs are blocked by the CSP:\n  " + "\n  ".join(offenders)


def test_the_scan_actually_detects_a_violation(tmp_path, monkeypatch):
    """Guards the guard: a passing scan must mean "clean", not "found nothing"."""
    monkeypatch.setattr(f"{__name__}.TEMPLATES_DIR", tmp_path)
    (tmp_path / "bad.html").write_text(
        '<form onsubmit="return confirm(\'x\')"><script>alert(1)</script></form>',
        encoding="utf-8",
    )
    assert _offenders(INLINE_HANDLER)
    assert _offenders(INLINE_SCRIPT)
