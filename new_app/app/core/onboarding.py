"""Website onboarding state machine.

A new website starts in DRAFT and must never become ACTIVE automatically —
it only reaches ACTIVE by passing through (or manually fast-tracking past)
review states. Real pattern detection doesn't exist yet, so DETECTING/
DETECTED/UNSUPPORTED/FAILING are schema-complete and transition-validated now,
ready for a future phase's extraction engine to drive them automatically —
today they're only reachable via explicit admin action.
"""

DRAFT = "draft"
DETECTING = "detecting"
DETECTED = "detected"
NEEDS_REVIEW = "needs_review"
APPROVED = "approved"
ACTIVE = "active"
INACTIVE = "inactive"
UNSUPPORTED = "unsupported"
FAILING = "failing"
ARCHIVED = "archived"

ONBOARDING_STATES: tuple[str, ...] = (
    DRAFT,
    DETECTING,
    DETECTED,
    NEEDS_REVIEW,
    APPROVED,
    ACTIVE,
    INACTIVE,
    UNSUPPORTED,
    FAILING,
    ARCHIVED,
)

# Allowed next-states per current state. APPROVED is reachable directly from
# every pre-approval state (not just NEEDS_REVIEW) as a manual fast-track —
# there's no real detection pipeline yet, so admins need a way to hand-approve
# a site they've configured themselves via `configuration`/`approved_pattern`.
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    DRAFT: frozenset({DETECTING, APPROVED, ARCHIVED}),
    DETECTING: frozenset({DETECTED, UNSUPPORTED, APPROVED, ARCHIVED}),
    DETECTED: frozenset({NEEDS_REVIEW, UNSUPPORTED, APPROVED, ARCHIVED}),
    NEEDS_REVIEW: frozenset({APPROVED, UNSUPPORTED, ARCHIVED}),
    APPROVED: frozenset({ACTIVE, ARCHIVED}),
    ACTIVE: frozenset({INACTIVE, FAILING, ARCHIVED}),
    INACTIVE: frozenset({ACTIVE, ARCHIVED}),
    UNSUPPORTED: frozenset({DRAFT, ARCHIVED}),
    FAILING: frozenset({ACTIVE, INACTIVE, ARCHIVED}),
    ARCHIVED: frozenset(),
}

# Permission required to move a website *into* a given target status.
TRANSITION_PERMISSIONS: dict[str, str] = {
    DRAFT: "sites.update",
    DETECTING: "sites.test",
    DETECTED: "sites.test",
    NEEDS_REVIEW: "sites.update",
    APPROVED: "sites.approve",
    ACTIVE: "sites.activate",
    INACTIVE: "sites.activate",
    UNSUPPORTED: "sites.update",
    FAILING: "sites.update",
    ARCHIVED: "sites.archive",
}


def can_transition(current: str, target: str) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())
