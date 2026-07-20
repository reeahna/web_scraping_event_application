"""Unsupported-site-report lifecycle state machine.

Mirrors app.core.onboarding's shape: a small set of named states plus an
explicit transition table, so report status changes are validated the same
way website onboarding transitions are — never an arbitrary string write.
"""

OPEN = "open"
INVESTIGATING = "investigating"
WAITING_FOR_BROWSER_SUPPORT = "waiting_for_browser_support"
CONFIGURATION_CREATED = "configuration_created"
RESOLVED = "resolved"
DISMISSED = "dismissed"

REPORT_STATUSES: tuple[str, ...] = (
    OPEN,
    INVESTIGATING,
    WAITING_FOR_BROWSER_SUPPORT,
    CONFIGURATION_CREATED,
    RESOLVED,
    DISMISSED,
)

# Allowed next-states per current state. RESOLVED/DISMISSED can both reopen
# back to OPEN — "reopen when appropriate" per spec — but never jump directly
# into each other (a dismissed report must be reopened before it can be
# resolved, and vice versa, so the transition is always explicit).
ALLOWED_REPORT_TRANSITIONS: dict[str, frozenset[str]] = {
    OPEN: frozenset({INVESTIGATING, WAITING_FOR_BROWSER_SUPPORT, RESOLVED, DISMISSED}),
    INVESTIGATING: frozenset(
        {OPEN, WAITING_FOR_BROWSER_SUPPORT, CONFIGURATION_CREATED, RESOLVED, DISMISSED}
    ),
    WAITING_FOR_BROWSER_SUPPORT: frozenset({OPEN, INVESTIGATING, RESOLVED, DISMISSED}),
    CONFIGURATION_CREATED: frozenset({OPEN, INVESTIGATING, RESOLVED, DISMISSED}),
    RESOLVED: frozenset({OPEN}),
    DISMISSED: frozenset({OPEN}),
}


def can_transition_report(current: str, target: str) -> bool:
    return target in ALLOWED_REPORT_TRANSITIONS.get(current, frozenset())
