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
from app.core.onboarding import ACTIVE, APPROVED, FAILING, INACTIVE, can_transition
from app.models.website import Website
from app.schemas.extraction import SiteConfiguration

_ALREADY_APPROVED_OR_BEYOND = frozenset({APPROVED, ACTIVE, INACTIVE, FAILING})


def save_draft_configuration(
    db: Session, website: Website, configuration: SiteConfiguration
) -> Website:
    website.configuration = configuration.model_dump(mode="json")
    website.configuration_version += 1
    db.commit()
    db.refresh(website)
    return website


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
