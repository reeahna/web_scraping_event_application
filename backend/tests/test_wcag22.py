"""Static guards for the WCAG 2.2 AA fixes that live in templates and CSS.

The full audit runs axe-core in a browser, which the test suite does not; these
checks catch the regressions that are visible in the source alone.
"""

import re

from app.paths import STATIC_DIR, TEMPLATES_DIR

STYLE = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
EMPTY_HEADER = re.compile(r"<th(\s[^>]*)?>\s*</th>", re.I)


def test_no_table_header_is_empty():
    # A screen reader announces an empty header as nothing, so the column of
    # buttons under it has no name; label it, visually hidden if need be.
    offenders = [
        str(t.relative_to(TEMPLATES_DIR))
        for t in sorted(TEMPLATES_DIR.rglob("*.html"))
        if EMPTY_HEADER.search(t.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"empty <th> in: {offenders}"


def test_focus_is_not_hidden_under_the_sticky_header():
    # 2.4.11 Focus Not Obscured: Shift+Tab scrolls the focused element to the
    # top edge, under the sticky header, unless the scroll port is padded.
    assert "position: sticky" in STYLE
    assert re.search(r"html\s*\{[^}]*scroll-padding-top", STYLE)


def test_checkboxes_are_not_browser_default_size():
    # 2.5.8 Target Size: the default 13px checkbox is under 24px. Any rule that
    # sizes a checkbox back to auto undoes the base rule.
    assert re.search(r'input\[type="checkbox"\][^{]*\{[^}]*width:\s*20px', STYLE)
    assert not re.search(r'input\[type="checkbox"\]\s*\{[^}]*width:\s*auto', STYLE)
