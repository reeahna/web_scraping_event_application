"""Vocabulary for policy-controlled automatic onboarding.

Deliberately separate from `app.core.onboarding` (Website lifecycle) and
`app.core.onboarding_jobs` (queue outcomes). A decision says what the policy
concluded; it does not say what state the Website is in. Keeping the three
vocabularies apart is what stops "approved" the decision from being confused
with "approved" the lifecycle state.
"""

# --- What produced the evaluation -----------------------------------------
DECISION_ONBOARDING = "onboarding"
DECISION_REEVALUATION = "reevaluation"

DECISION_KINDS: tuple[str, ...] = (DECISION_ONBOARDING, DECISION_REEVALUATION)

# --- Final decisions -------------------------------------------------------
NO_APPLICABLE_POLICY = "no_applicable_policy"
POLICY_DISABLED = "policy_disabled"
AUTOMATIC_APPROVAL_DENIED = "automatic_approval_denied"
AUTOMATIC_APPROVAL_ALLOWED = "automatic_approval_allowed"
AUTOMATICALLY_APPROVED = "automatically_approved"
AUTOMATIC_ACTIVATION_DENIED = "automatic_activation_denied"
AUTOMATIC_ACTIVATION_ALLOWED = "automatic_activation_allowed"
AUTOMATICALLY_ACTIVATED = "automatically_activated"
MANUAL_REVIEW_REQUIRED = "manual_review_required"
ACTION_FAILED = "action_failed"

FINAL_DECISIONS: tuple[str, ...] = (
    NO_APPLICABLE_POLICY,
    POLICY_DISABLED,
    AUTOMATIC_APPROVAL_DENIED,
    AUTOMATIC_APPROVAL_ALLOWED,
    AUTOMATICALLY_APPROVED,
    AUTOMATIC_ACTIVATION_DENIED,
    AUTOMATIC_ACTIVATION_ALLOWED,
    AUTOMATICALLY_ACTIVATED,
    MANUAL_REVIEW_REQUIRED,
    ACTION_FAILED,
)

# Decisions that mean "a human still has to look at this". Used by callers to
# decide whether to leave a job in its manual-review outcome.
MANUAL_REVIEW_DECISIONS: frozenset[str] = frozenset(
    {
        NO_APPLICABLE_POLICY,
        POLICY_DISABLED,
        AUTOMATIC_APPROVAL_DENIED,
        MANUAL_REVIEW_REQUIRED,
        ACTION_FAILED,
    }
)

# --- Where a configuration came from ---------------------------------------
# Tracked on the Website so a policy can refuse to auto-approve anything it
# did not deterministically derive itself.
ORIGIN_DETERMINISTIC_STRUCTURED = "deterministic_structured"
ORIGIN_DETERMINISTIC_GENERIC_HTML = "deterministic_generic_html"
ORIGIN_ADMINISTRATOR_MANUAL = "administrator_manual"
ORIGIN_AI_SUGGESTED = "ai_suggested"
ORIGIN_IMPORTED = "imported_configuration"

CONFIGURATION_ORIGINS: tuple[str, ...] = (
    ORIGIN_DETERMINISTIC_STRUCTURED,
    ORIGIN_DETERMINISTIC_GENERIC_HTML,
    ORIGIN_ADMINISTRATOR_MANUAL,
    ORIGIN_AI_SUGGESTED,
    ORIGIN_IMPORTED,
)

# --- Actions taken as a result of a decision -------------------------------
ACTION_APPROVAL = "approval"
ACTION_ACTIVATION = "activation"

ACTION_TYPES: tuple[str, ...] = (ACTION_APPROVAL, ACTION_ACTIVATION)

# --- Who acted -------------------------------------------------------------
# `system` never corresponds to a User row. There is no system account, no
# password, and no session: a system action happens only because an active
# administrator-created policy permitted it, and the administrator who wrote
# that policy is recorded on the policy itself.
ACTOR_USER = "user"
ACTOR_SYSTEM = "system"

ACTOR_TYPES: tuple[str, ...] = (ACTOR_USER, ACTOR_SYSTEM)

SYSTEM_ACTOR_LABEL = "automatic_onboarding_policy"

DEFAULT_POLICY_NAME = "Conservative default"
