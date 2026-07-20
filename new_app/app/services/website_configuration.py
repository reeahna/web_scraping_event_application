"""Configuration draft/approval workflow for one website's extraction setup.

`Website.configuration` is the admin's editable draft. `Website.
approved_pattern` is a frozen, self-contained snapshot copied from the draft
at approval time — app.services.extraction_runs.run_extraction reads only
the frozen snapshot, never the live draft, so editing the draft after
approval has zero effect on persistent extraction until an explicit
re-approve.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.exceptions import AppError
from app.core.onboarding import (
    ACTIVE,
    APPROVED,
    ARCHIVED,
    DRAFT,
    FAILING,
    INACTIVE,
    NEEDS_REVIEW,
    UNSUPPORTED,
    can_transition,
)
from app.extraction.registry import REGISTRY, UnsupportedPatternError
from app.models.website import Website
from app.repositories.extraction_run import get_latest_run_for_website
from app.schemas.extraction import SiteConfiguration
from app.services.notifications import SEVERITY_WARNING, build_dedup_fingerprint, notify
from app.services.rbac import users_with_permission

_ALREADY_APPROVED_OR_BEYOND = frozenset({APPROVED, ACTIVE, INACTIVE, FAILING})
_MANUAL_SELECTION_SOURCE_STATES = frozenset({DRAFT, UNSUPPORTED, NEEDS_REVIEW})


def save_draft_configuration(
    db: Session, website: Website, configuration: SiteConfiguration
) -> Website:
    website.configuration = configuration.model_dump(mode="json")
    website.configuration_version += 1
    db.commit()
    db.refresh(website)
    return website


def select_pattern(db: Session, website: Website, *, pattern_name: str) -> Website:
    """Lets an administrator manually choose a registered pattern when
    detection was low-confidence or unsupported. Writes a scaffold draft
    configuration (through save_draft_configuration, so configuration_version
    bumps normally) and moves the website into NEEDS_REVIEW so it still goes
    through the normal preview/approve flow — this never bypasses either."""
    try:
        REGISTRY.get(pattern_name)
    except UnsupportedPatternError as exc:
        raise AppError(str(exc), status_code=400) from exc

    if website.onboarding_status not in _MANUAL_SELECTION_SOURCE_STATES:
        raise AppError(
            f"Cannot manually select a pattern from state '{website.onboarding_status}'",
            status_code=409,
        )

    scaffold_kwargs: dict = {"pattern_name": pattern_name}
    if pattern_name == "wordpress_rest" and website.event_listing_url:
        scaffold_kwargs["api_endpoint"] = website.event_listing_url
    else:
        scaffold_kwargs["listing_url"] = website.event_listing_url or website.base_url
    save_draft_configuration(db, website, SiteConfiguration(**scaffold_kwargs))

    if can_transition(website.onboarding_status, NEEDS_REVIEW):
        website.onboarding_status = NEEDS_REVIEW
        website.is_active = False
        db.commit()
        db.refresh(website)
    return website


def _require_current_preview(db: Session, website: Website) -> None:
    """Approval must not use a stale or absent preview. Because
    `configuration_version` is bumped on every draft save, a version match
    between the latest preview run and the website's current draft already
    implies the pattern name and listing/API endpoint are unchanged too —
    no separate columns or separate checks are needed for those."""
    latest_preview = get_latest_run_for_website(db, website.id, run_type="preview")
    if latest_preview is None:
        raise AppError(
            "A successful preview of the current configuration is required before approval.",
            status_code=409,
        )
    if latest_preview.configuration_version != website.configuration_version:
        raise AppError(
            "The configuration changed after the last preview. Run preview again before approving.",
            status_code=409,
        )
    if latest_preview.status not in ("success", "partial"):
        raise AppError(
            f"The latest preview did not complete successfully (status: "
            f"{latest_preview.status}). Run preview again before approving.",
            status_code=409,
        )


def approve_configuration(db: Session, website: Website, *, approved_by_user_id: int) -> Website:
    """Copies the current draft `configuration` into `approved_pattern`. A
    website must not become ACTIVE solely from this action — only the
    pre-existing, separately-permissioned `sites.activate` transition does
    that; approving only ever reaches (at most) the APPROVED onboarding
    state."""
    if not website.configuration:
        raise AppError(
            "No draft configuration to approve — save a configuration first.", status_code=409
        )
    if website.onboarding_status == ARCHIVED:
        raise AppError("Cannot approve a configuration for an archived website.", status_code=409)

    config = SiteConfiguration.model_validate(website.configuration)
    try:
        registration = REGISTRY.get(config.pattern_name)
    except UnsupportedPatternError as exc:
        raise AppError(str(exc), status_code=409) from exc
    if registration.browser_required:
        raise AppError(
            "This pattern requires browser rendering, which is not supported yet. "
            "Configuration cannot be approved.",
            status_code=409,
        )
    proposed_detection = (website.proposed_pattern or {}).get("detection") or {}
    if proposed_detection.get("browser_required"):
        raise AppError(
            "The latest detection indicated this site requires browser rendering, which is "
            "not supported yet. Configuration cannot be approved.",
            status_code=409,
        )

    if website.city_id is None or website.city is None or not website.city.is_active:
        raise AppError(
            "Website must be assigned to an active city before approval.", status_code=409
        )

    _require_current_preview(db, website)

    if website.onboarding_status not in _ALREADY_APPROVED_OR_BEYOND:
        if not can_transition(website.onboarding_status, APPROVED):
            raise AppError(
                f"Cannot approve a configuration from state '{website.onboarding_status}'",
                status_code=409,
            )
        website.onboarding_status = APPROVED
        website.is_active = False

    website.approved_pattern = dict(website.configuration)
    website.approved_at = datetime.now(UTC)
    website.approved_by_user_id = approved_by_user_id
    website.active_configuration_version = website.configuration_version
    db.commit()
    db.refresh(website)
    return website


def reject_configuration(
    db: Session, website: Website, *, reason: str, correlation_id: str | None = None
) -> Website:
    """Two distinct cases, per spec:

    - Rejecting *initial* onboarding (no approved_pattern yet): the proposed
      configuration wasn't acceptable. Detector evidence (proposed_pattern)
      and any preview runs/unsupported reports are left untouched — only the
      draft configuration is cleared, and the website moves to UNSUPPORTED
      when that's a legal transition from its current state (mirroring how
      a failed detection already lands there).
    - Rejecting a *revised draft* for an already-approved website: must not
      touch onboarding_status/is_active (an active site stays active) and
      must not touch approved_pattern (the prior approved configuration is
      preserved). The pending draft edit is simply discarded back to the
      last approved snapshot.
    """
    reason = reason.strip()
    if not reason:
        raise AppError("A rejection reason is required.", status_code=400)

    if website.approved_pattern:
        website.configuration = dict(website.approved_pattern)
        website.configuration_version += 1
    else:
        website.configuration = None
        if can_transition(website.onboarding_status, UNSUPPORTED):
            website.onboarding_status = UNSUPPORTED
            website.is_active = False

    db.commit()
    db.refresh(website)

    notify(
        db,
        notification_type="configuration_rejected",
        severity=SEVERITY_WARNING,
        title=f"{website.name}: configuration rejected",
        message=f"The proposed configuration for '{website.name}' was rejected: {reason}",
        recipients=users_with_permission(db, "sites.approve"),
        related_resource_type="website",
        related_resource_id=website.id,
        action_url=f"/admin/websites/{website.id}",
        dedup_fingerprint=build_dedup_fingerprint(
            "configuration_rejected", str(website.id), str(website.configuration_version)
        ),
        correlation_id=correlation_id,
    )
    return website
