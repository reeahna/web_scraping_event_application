"""Helpers for free-text search filters."""

# The escape character contains_pattern() uses; pass it to ilike(escape=...).
LIKE_ESCAPE = "\\"


def contains_pattern(term: str) -> str:
    """A LIKE pattern matching `term` anywhere, with the term's own % and _
    taken literally rather than as wildcards (so "%" does not match everything)."""
    escaped = (
        term.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2).replace("%", r"\%").replace("_", r"\_")
    )
    return f"%{escaped}%"
