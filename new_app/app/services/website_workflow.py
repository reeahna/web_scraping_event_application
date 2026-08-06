"""Presentation-only workflow model for the Website admin detail page.

This module computes *what the administrator should do next* from the current
Website state and renders it as a small, deterministic view model. It performs
no I/O and changes no behaviour: lifecycle rules, approval rules, extraction,
policy, permissions, CSRF, and persistence all stay exactly where they are. The
template renders this model instead of deriving workflow state through many
Jinja conditions.

The workflow is a fixed six-step line — Detect → Configure → Preview → Review →
Approve → Run — and there is always at most one primary next action. Everything
else (secondary, lifecycle, advanced, destructive) is deliberately kept off the
primary path.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Step keys, in order.
STEPS: tuple[str, ...] = ("detect", "configure", "preview", "review", "approve", "run")
STEP_LABELS = {
    "detect": "Detect",
    "configure": "Configure",
    "preview": "Preview",
    "review": "Review",
    "approve": "Approve",
    "run": "Run",
}

# Step/action states.
COMPLETE, CURRENT, BLOCKED, FAILED, NOT_STARTED = (
    "complete", "current", "blocked", "failed", "not_started"
)


@dataclass(frozen=True)
class WorkflowAction:
    label: str
    url: str
    method: str = "post"  # "post" | "get"
    style: str = "secondary"  # "primary" | "secondary" | "muted" | "danger"
    enabled: bool = True
    disabled_reason: str | None = None
    confirmation_text: str | None = None
    # For POST status transitions.
    status_target: str | None = None


@dataclass(frozen=True)
class WorkflowStep:
    key: str
    label: str
    state: str
    explanation: str = ""
    blocked_reason: str | None = None


@dataclass
class WebsiteWorkflow:
    current_step: str | None
    steps: list[WorkflowStep]
    headline: str
    explanation: str
    primary_action: WorkflowAction | None
    secondary_actions: list[WorkflowAction] = field(default_factory=list)
    lifecycle_actions: list[WorkflowAction] = field(default_factory=list)
    lifecycle_notes: list[str] = field(default_factory=list)
    advanced_actions: list[WorkflowAction] = field(default_factory=list)
    danger_actions: list[WorkflowAction] = field(default_factory=list)
    approval_blockers: list[str] = field(default_factory=list)
    run_blockers: list[str] = field(default_factory=list)
    config_is_failed_historical: bool = False
    config_warning: str | None = None
    # Authoritative display guard for the approved lifecycle transition: false
    # whenever the current configuration is not approvable.
    approval_allowed: bool = False
    preview_matches_current: bool = False
    preview_status_label: str = "No preview yet"
    # Grouped validation errors (message, count) for the preview card, plus the
    # original raw candidate-level errors for the collapsed technical view.
    preview_error_groups: list[tuple[str, int]] = field(default_factory=list)
    preview_raw_errors: list[str] = field(default_factory=list)
    show_recovery_card: bool = False
    warnings: list[str] = field(default_factory=list)


_LIFECYCLE_LABELS = {
    "draft": ("Move to draft", "Return this source to configuration work."),
    "detecting": ("Move to detecting", "Mark the source as awaiting detection."),
    "detected": ("Move to detected", "Mark detection as complete."),
    "needs_review": ("Move to needs review", "Mark the source for administrator review."),
    "unsupported": ("Move to unsupported", "Mark the source as not currently supported."),
    "approved": (
        "Fast-track lifecycle to approved",
        "Advanced: sets the lifecycle status directly. The guided 'Approve "
        "configuration' action is preferred.",
    ),
    "inactive": ("Deactivate", "Stop scheduled imports; keep the approved configuration."),
    "failing": ("Move to failing", "Mark the source as failing."),
    "archived": ("Archive", "Disable normal processing while preserving history."),
}


def _preview_status_label(preview, matches: bool) -> str:
    if preview is None:
        return "No preview yet"
    if not matches:
        return "Preview does not match the current configuration"
    status = preview.status
    valid = preview.events_valid
    if status in ("success", "partial") and valid > 0:
        return "Preview passed"
    if status == "needs_review":
        return "Preview needs review"
    if status == "blocked":
        return "Preview blocked"
    if status == "failed" or valid == 0:
        return "Preview failed"
    return f"Preview {status}"


def _def(url_id: int, path: str) -> str:
    return f"/admin/websites/{url_id}/{path}"


def _group_preview_errors(preview) -> tuple[list[tuple[str, int]], list[str]]:
    """Group a preview run's semicolon-joined `error_summary` by message so the
    card can show '4 candidates: no parseable start date' instead of one long
    paragraph. Returns (grouped, raw); the raw list is preserved for the
    collapsed technical view. Never discards the original errors."""
    if preview is None or not getattr(preview, "error_summary", None):
        return [], []
    raw = [part.strip() for part in preview.error_summary.split(";") if part.strip()]
    counts: dict[str, int] = {}
    order: list[str] = []
    for item in raw:
        # Drop the "candidate[N]: " prefix so identical errors group together.
        message = item.split(":", 1)[1].strip() if item.startswith("candidate[") else item
        if message not in counts:
            counts[message] = 0
            order.append(message)
        counts[message] += 1
    grouped = sorted(((m, counts[m]) for m in order), key=lambda mc: (-mc[1], mc[0]))
    return grouped, raw


def build_website_workflow(
    *,
    website,
    latest_preview_run,
    latest_browser_recovery,
    next_states,
    permissions: dict,
    browser_extraction_enabled: bool,
) -> WebsiteWorkflow:
    """Compute the presentation workflow. `permissions` carries the already
    server-checked booleans (update/test/approve/delete); `next_states` is the
    already permission-filtered set of valid lifecycle transitions."""
    wid = website.id
    status = website.onboarding_status
    archived = website.archived_at is not None
    can_update = permissions.get("update", False)
    can_test = permissions.get("test", False)
    can_approve = permissions.get("approve", False)
    can_delete = permissions.get("delete", False)

    proposed = website.proposed_pattern or {}
    detection = proposed.get("detection") or {}
    has_detection = bool(detection) or bool(website.configuration)
    has_draft = website.configuration is not None
    draft_pattern = (website.configuration or {}).get("pattern_name") if has_draft else None
    draft_version = website.configuration_version
    approved = website.approved_pattern is not None

    recovery = latest_browser_recovery or None
    recovery_new_pattern_needed = bool(recovery and recovery.get("new_pattern_needed"))

    preview = latest_preview_run
    preview_matches = bool(preview and preview.configuration_version == draft_version)
    preview_status = preview.status if preview else None
    preview_valid = preview.events_valid if preview else 0
    matching_success = (
        preview_matches and preview_status in ("success", "partial") and preview_valid > 0
    )
    matching_failed = preview_matches and (
        preview_status in ("failed", "blocked") or (preview is not None and preview_valid == 0)
    )
    preview_stale = bool(preview) and not preview_matches

    # A stored draft is "failed historical" when it is not from the latest,
    # successful work: the latest recovery preferred a different endpoint (and
    # created no draft), or the source is unresolved and this draft's own
    # preview failed.
    config_is_failed_historical = has_draft and not approved and (
        recovery_new_pattern_needed
        or (status in ("unsupported", "needs_review") and matching_failed)
    )
    config_usable = has_draft and not config_is_failed_historical

    city_active = bool(website.city and website.city.is_active)
    detection_browser_required = bool(detection.get("browser_required"))

    # --- approval blockers (mirror the server rules, presentation only) -----
    approval_blockers: list[str] = []
    if approved:
        pass
    else:
        if not can_approve:
            approval_blockers.append("You do not have permission to approve configurations.")
        if not has_draft:
            approval_blockers.append("No draft configuration exists yet.")
        elif config_is_failed_historical:
            approval_blockers.append(
                f"Configuration version {draft_version} is a failed historical "
                f"{draft_pattern} draft and is not eligible for approval."
            )
        if not city_active:
            approval_blockers.append("The source has no active city.")
        if detection_browser_required:
            approval_blockers.append(
                "The configuration requires browser rendering, which cannot be approved."
            )
        if preview is None:
            approval_blockers.append("No preview has been run for the current configuration.")
        elif not preview_matches:
            approval_blockers.append(
                f"The latest preview was for configuration version "
                f"{preview.configuration_version}, not the current version {draft_version}."
            )
        elif preview_status not in ("success", "partial"):
            approval_blockers.append(f"The latest preview {preview_status}.")
        elif preview_valid == 0:
            approval_blockers.append("The latest preview produced no valid events.")
    approval_allowed = not approved and not approval_blockers

    # --- run blockers -------------------------------------------------------
    run_blockers: list[str] = []
    if archived:
        run_blockers.append("Event import is unavailable because the source is archived.")
    if not approved:
        run_blockers.append("Event import is unavailable until the configuration is approved.")
    elif not website.is_active:
        run_blockers.append("Event import is unavailable until the source is activated.")
    run_ready = approved and website.is_active and not archived

    # --- decide the current step + primary action ---------------------------
    browser_retry_available = browser_extraction_enabled and can_test and not archived
    primary: WorkflowAction | None = None
    secondary: list[WorkflowAction] = []
    current_step: str | None = None
    headline = status.replace("_", " ").title()
    explanation = ""
    primary_status_target: str | None = None

    def action(label, path, **kw):
        return WorkflowAction(label=label, url=_def(wid, path), **kw)

    if archived:
        current_step = None
        headline = "Archived"
        explanation = (
            "This source is archived. Normal processing is disabled and its history is preserved."
        )
    elif status in ("draft", "detecting") and not has_draft and not has_detection:
        current_step = "detect"
        headline = "Not detected yet"
        explanation = "Detect events automatically to identify a supported source and configure it."
        if can_test:
            primary = action("Detect events automatically", "detect-and-configure", style="primary")
    elif status == "unsupported" and not config_usable:
        current_step = "configure"
        headline = "Unsupported"
        if browser_retry_available:
            explanation = (
                "Ordinary detection could not identify a supported source. Retry automatic "
                "recovery to render the page and re-run detection against every registered pattern."
            )
            primary = action("Retry automatic recovery", "browser-retry", style="primary")
        else:
            explanation = (
                "Ordinary detection could not identify a supported source. Browser recovery is "
                "unavailable, so configure the source manually from Advanced tools."
            )
    elif status == "needs_review" and (recovery_new_pattern_needed or not config_usable):
        current_step = "configure"
        headline = "Needs review"
        if recovery_new_pattern_needed:
            explanation = (
                "A preferred structured event endpoint was found, but the stored draft is not "
                "from it. Retry automatic recovery to re-run detection with the current pattern "
                "registry and generate a configuration."
            )
        else:
            explanation = "This source needs a usable configuration. Retry automatic recovery."
        if browser_retry_available:
            primary = action("Retry automatic recovery", "browser-retry", style="primary")
    elif config_usable and not approved:
        # A usable draft exists: drive the preview → review → approve path.
        if preview is None or preview_stale:
            current_step = "preview"
            if preview_stale:
                headline = "Preview out of date"
                explanation = (
                    "The configuration changed since the last preview. Run a new preview to test "
                    "the current configuration."
                )
                label = "Run a new preview"
            else:
                headline = "Ready to preview"
                explanation = "A draft configuration is ready. Test it with a preview."
                label = "Test with preview"
            if can_test:
                primary = action(label, "preview-extraction", style="primary")
        elif matching_failed:
            current_step = "preview"
            headline = "Preview failed"
            explanation = (
                "The latest preview produced no valid events, so the configuration must be "
                "corrected before it can be approved."
            )
            if can_update:
                primary = action("Fix configuration", "configure", method="get", style="primary")
        elif approval_allowed:
            current_step = "approve"
            headline = "Ready to approve"
            explanation = "The preview passed. Review the results, then approve the configuration."
            primary = action(
                "Approve configuration", "approve-configuration", style="primary",
                confirmation_text=f"Approve the {draft_pattern} configuration for {website.name}?",
            )
            secondary.append(
                action("Review preview results", "onboarding", method="get", style="secondary")
            )
        else:
            current_step = "review"
            headline = "Review preview"
            explanation = (
                "The preview completed but approval is blocked. Review the results and the "
                "blocking reasons below."
            )
            primary = action("Review preview results", "onboarding", method="get", style="primary")
    elif approved and not website.is_active:
        current_step = "run"
        headline = "Approved — not active"
        explanation = "The configuration is approved. Activate the source to enable event imports."
        if "active" in next_states:
            primary = action(
                "Activate source", "status", style="primary", status_target="active",
                confirmation_text=f"Activate {website.name}?",
            )
            primary_status_target = "active"
    elif approved and website.is_active:
        current_step = "run"
        headline = "Failing" if status == "failing" else "Approved and active"
        if status == "failing":
            explanation = (
                f"Recent imports failed ({website.consecutive_failure_count} consecutive). "
                "Retry an event import once the source is healthy."
            )
        else:
            explanation = "The source is approved and active. Run an event import when ready."
        if can_test and run_ready:
            primary = action("Run event import", "run-extraction", style="primary")
    else:
        # Fallback (e.g. detected/needs_review with no draft yet and no recovery).
        current_step = "configure"
        headline = status.replace("_", " ").title()
        explanation = "Continue configuring this source. See the workflow steps below."
        if can_test:
            primary = action(
                "Detect events automatically", "detect-and-configure", style="primary"
            )

    # Edit is always a secondary (never a header primary).
    if can_update:
        secondary.append(action("Edit website", "edit", method="get", style="secondary"))
        if has_draft:
            # A failed/historical draft is inspected, not "reviewed" toward
            # approval — the label must not imply it is progressing.
            review_label = (
                "Inspect failed configuration"
                if config_is_failed_historical
                else "Review configuration"
            )
            secondary.append(action(review_label, "configure", method="get", style="muted"))

    # --- per-step states ----------------------------------------------------
    completion = {
        "detect": has_detection or has_draft or approved,
        "configure": config_usable or approved,
        "preview": matching_success or approved,
        "review": approved,
        "approve": approved,
        "run": website.last_success_at is not None,
    }
    step_explanations = {
        "detect": "Identify a supported source automatically.",
        "configure": "Produce a complete, usable configuration.",
        "preview": "Run the configuration without persisting events and score the result.",
        "review": "Check the preview results and quality before approving.",
        "approve": "Freeze the configuration as the approved snapshot.",
        "run": "Import events on the approved configuration.",
    }
    steps: list[WorkflowStep] = []
    current_index = STEPS.index(current_step) if current_step in STEPS else len(STEPS)
    for i, key in enumerate(STEPS):
        blocked_reason = None
        if completion[key]:
            state = COMPLETE
        elif key == current_step:
            state = FAILED if (key == "preview" and matching_failed) else CURRENT
        elif i < current_index:
            state = COMPLETE
        else:
            state = BLOCKED
            if key == "approve":
                blocked_reason = (
                    approval_blockers[0] if approval_blockers else "Preview must pass first."
                )
            elif key == "run":
                blocked_reason = (
                    run_blockers[0] if run_blockers else "Approve the configuration first."
                )
            elif key == "preview" and not config_usable:
                blocked_reason = "A usable configuration is required first."
            else:
                blocked_reason = "Complete the previous steps first."
        if archived:
            state = COMPLETE if completion[key] else BLOCKED
            blocked_reason = "The source is archived." if state == BLOCKED else None
        steps.append(
            WorkflowStep(
                key=key, label=STEP_LABELS[key], state=state,
                explanation=step_explanations[key], blocked_reason=blocked_reason,
            )
        )

    # --- lifecycle / advanced / danger actions ------------------------------
    lifecycle: list[WorkflowAction] = []
    lifecycle_notes: list[str] = []
    for target in next_states:
        if target == primary_status_target:
            continue  # activation is surfaced as the primary, don't duplicate
        # Never present a direct "Move to approved" lifecycle jump while the
        # configuration is not approvable — it contradicts the approval blockers.
        # Presentation filtering only; the server transition rule is unchanged.
        if target == "approved" and not approval_allowed:
            lifecycle_notes.append(
                "Lifecycle approval is unavailable while the current configuration "
                "is not approvable."
            )
            continue
        label, effect = _LIFECYCLE_LABELS.get(
            target, (f"Move to {target}", "Change the lifecycle status.")
        )
        lifecycle.append(
            WorkflowAction(
                label=label, url=_def(wid, "status"), style="secondary",
                status_target=target, disabled_reason=effect,
                confirmation_text=f"Move {website.name} to '{target}'?",
            )
        )

    # Invariant (defence in depth): whenever approval is not allowed, no
    # lifecycle action targeting `approved` may survive — regardless of how the
    # list above was built. The loop already skips it; this guarantees it even
    # if a future edit adds an approved action by another path.
    if not approval_allowed:
        lifecycle = [a for a in lifecycle if a.status_target != "approved"]

    advanced: list[WorkflowAction] = []
    if can_test and not archived:
        advanced.append(action("Detect pattern only", "detect-pattern", style="muted"))
        advanced.append(
            action("Detect and configure (re-run)", "detect-and-configure", style="muted")
        )
    if can_update:
        advanced.append(
            action("Configure manually", "configure", method="get", style="muted")
        )

    danger: list[WorkflowAction] = []
    if can_delete:
        danger.append(action("Delete website…", "delete", method="get", style="danger"))

    # --- config warning + recovery visibility -------------------------------
    config_warning = None
    if config_is_failed_historical:
        config_warning = (
            f"Configuration version {draft_version} is a failed historical {draft_pattern} draft. "
            "It is not eligible for approval and was not produced by the latest recovery result."
        )

    show_recovery_card = bool(
        recovery is not None or status in ("unsupported", "needs_review")
    )

    warnings: list[str] = []
    if config_is_failed_historical:
        warnings.append("The stored configuration is historical and should not be approved.")

    preview_error_groups, preview_raw_errors = _group_preview_errors(preview)

    return WebsiteWorkflow(
        current_step=current_step,
        steps=steps,
        headline=headline,
        explanation=explanation,
        primary_action=primary,
        secondary_actions=secondary,
        lifecycle_actions=lifecycle,
        lifecycle_notes=lifecycle_notes,
        advanced_actions=advanced,
        danger_actions=danger,
        approval_blockers=approval_blockers,
        run_blockers=run_blockers,
        config_is_failed_historical=config_is_failed_historical,
        config_warning=config_warning,
        approval_allowed=approval_allowed,
        preview_matches_current=preview_matches,
        preview_status_label=_preview_status_label(preview, preview_matches),
        preview_error_groups=preview_error_groups,
        preview_raw_errors=preview_raw_errors,
        show_recovery_card=show_recovery_card,
        warnings=warnings,
    )
