"""Bulk-onboarding job and batch status vocabulary.

Deliberately ONE authoritative status per job. `OnboardingJob.status` moves
through the processing steps and then stops on a terminal value — and that
terminal value *is* the job's outcome. There is no separate `outcome` column,
because two fields that must always agree are two fields that eventually
won't.

`OnboardingJob.current_step` is the only other status-ish field and has a
different job: it is a diagnostic breadcrumb recording the last step
attempted, retained after a failure so a `failed` job can say *where* it
broke (e.g. "previewing"). It is never read to decide behaviour.
"""

# --- Processing steps (transient) -----------------------------------------
QUEUED = "queued"
VALIDATING = "validating"
LOCATING_EXISTING = "locating_existing"
CREATING_WEBSITE = "creating_website"
DETECTING = "detecting"
CONFIGURING = "configuring"
PREVIEWING = "previewing"

# --- Terminal outcomes -----------------------------------------------------
READY_FOR_APPROVAL = "ready_for_approval"
NEEDS_REVIEW = "needs_review"
UNSUPPORTED = "unsupported"
BLOCKED = "blocked"
FAILED = "failed"
DUPLICATE = "duplicate"
CANCELLED = "cancelled"

PROCESSING_STATUSES: tuple[str, ...] = (
    QUEUED,
    VALIDATING,
    LOCATING_EXISTING,
    CREATING_WEBSITE,
    DETECTING,
    CONFIGURING,
    PREVIEWING,
)

TERMINAL_STATUSES: tuple[str, ...] = (
    READY_FOR_APPROVAL,
    NEEDS_REVIEW,
    UNSUPPORTED,
    BLOCKED,
    FAILED,
    DUPLICATE,
    CANCELLED,
)

JOB_STATUSES: tuple[str, ...] = (*PROCESSING_STATUSES, *TERMINAL_STATUSES)

# States a retry is allowed from. `blocked` is excluded on purpose: retrying a
# blocked or unsafe URL is an explicit administrator decision, not something a
# generic "retry failures" action should sweep up (see
# app.services.bulk_onboarding.retry_job).
RETRYABLE_STATUSES: frozenset[str] = frozenset({FAILED, NEEDS_REVIEW, UNSUPPORTED})

# Statuses that mean "this URL is already being worked on", used for
# open-job duplicate detection.
ACTIVE_STATUSES: frozenset[str] = frozenset(PROCESSING_STATUSES)


# --- Batch lifecycle -------------------------------------------------------
BATCH_OPEN = "open"
BATCH_COMPLETED = "completed"
BATCH_CANCELLED = "cancelled"

BATCH_STATUSES: tuple[str, ...] = (BATCH_OPEN, BATCH_COMPLETED, BATCH_CANCELLED)

SOURCE_SINGLE = "single"
SOURCE_PASTE = "paste"
SOURCE_CSV = "csv"

SOURCE_KINDS: tuple[str, ...] = (SOURCE_SINGLE, SOURCE_PASTE, SOURCE_CSV)


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES
