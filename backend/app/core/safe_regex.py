"""Shared ReDoS-safety validation for every admin-authored regular expression
in the app — categorization rules (Phase 5) and extraction transformation
rules (Phase 6) share the exact same risk: an admin-authored pattern run
against untrusted scraped/page text, where catastrophic backtracking would
hang the process. Promoted out of app.services.categorization so both call
sites share one implementation rather than drifting apart.
"""

import re

UNSAFE_REGEX_RE = re.compile(
    r"\(\?"  # lookaround, named, or non-capturing groups — "(?=", "(?:", "(?P<...>", etc.
    r"|\\[1-9]"  # backreferences
    r"|\([^)]*[+*|][^)]*\)\s*[+*{]"  # a group containing +, *, or | that is itself
    # quantified, e.g. "(a+)+" or "(a|ab)+" — the classic nested-quantifier/
    # alternation shape that causes catastrophic backtracking in Python's `re`.
)

MAX_PATTERN_LENGTH = 200


def validate_safe_regex(pattern: str, *, max_length: int = MAX_PATTERN_LENGTH) -> str:
    """Raises ValueError if `pattern` is too long, uses a disallowed
    high-risk construct, or doesn't compile. Returns `pattern` unchanged on
    success. Never executes the pattern against any input — this only
    inspects the pattern's own text."""
    if len(pattern) > max_length:
        raise ValueError(f"Regular expressions must be {max_length} characters or fewer")
    if UNSAFE_REGEX_RE.search(pattern):
        raise ValueError("Regular expression uses a disallowed high-risk construct")
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"Invalid regular expression: {exc}") from exc
    return pattern
