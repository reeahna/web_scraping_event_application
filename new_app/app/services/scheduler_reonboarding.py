"""Post-failure re-onboarding (Phase 10).

When an active source fails structurally several runs in a row (its markup or
API shape appears to have changed), the scheduler triggers this: rerun
detection, refresh the *draft* configuration, preview it, and notify reviewers
with a comparison of the approved pattern versus the freshly detected one.

Crucially, it NEVER silently replaces the approved configuration. Re-approval
stays an explicit, audited action through the existing approval / automatic-
onboarding paths; here we only surface that the source needs attention and give
reviewers fresh evidence. The approved snapshot — and therefore live extraction
— is untouched.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.website import Website
from app.services.notifications import SEVERITY_WARNING, build_dedup_fingerprint, notify

logger = get_logger("scheduler.reonboarding")


def _approved_pattern_name(website: Website) -> str | None:
    approved = website.approved_pattern or {}
    config = approved.get("configuration", approved) if isinstance(approved, dict) else {}
    if isinstance(config, dict):
        return config.get("pattern_name")
    return None


async def reonboard_after_structure_failures(db: Session, website: Website) -> bool:
    """Refresh detection + draft + preview and notify reviewers. Returns True
    when a re-onboarding review was raised. Never modifies approved_pattern."""
    from app.services.extraction_runs import preview_extraction, run_detection_detailed
    from app.services.rbac import users_with_permission

    approved_before = website.approved_pattern
    approved_pattern_name = _approved_pattern_name(website)

    detected_pattern_name: str | None = None
    preview_status = "not_run"
    try:
        detection = await run_detection_detailed(
            db, website, correlation_id=f"reonboard:{website.id}"
        )
        detected_pattern_name = detection.detection.pattern_name
    except Exception as exc:  # noqa: BLE001 - reonboarding is best-effort, never fatal
        logger.warning("reonboarding detection failed for website %s: %s", website.id, exc)

    # Preview only if detection produced a usable draft configuration.
    if website.proposed_pattern and website.proposed_pattern.get("configuration"):
        try:
            preview = await preview_extraction(
                db, website, correlation_id=f"reonboard-preview:{website.id}"
            )
            preview_status = preview.status
        except Exception as exc:  # noqa: BLE001
            logger.warning("reonboarding preview failed for website %s: %s", website.id, exc)
            preview_status = "error"

    # The approved configuration must be exactly as it was — assert we did not
    # touch it, and re-attach it in the unlikely event a sub-call cleared it.
    if website.approved_pattern is not approved_before:
        website.approved_pattern = approved_before
        db.commit()

    changed = detected_pattern_name != approved_pattern_name
    notify(
        db,
        notification_type="website_structure_reonboarding",
        severity=SEVERITY_WARNING,
        title=f"{website.name}: source structure changed, review needed",
        message=(
            f"'{website.name}' failed structurally on repeated scheduled runs. A fresh "
            f"detection was run: approved pattern '{approved_pattern_name}', "
            f"newly detected '{detected_pattern_name}' "
            f"({'changed' if changed else 'unchanged'}); draft preview status "
            f"'{preview_status}'. The approved configuration was NOT changed — "
            "re-approve explicitly if the new draft is correct."
        ),
        recipients=users_with_permission(db, "sites.approve"),
        related_resource_type="website",
        related_resource_id=website.id,
        action_url=f"/admin/websites/{website.id}",
        dedup_fingerprint=build_dedup_fingerprint(
            "website_structure_reonboarding", str(website.id), str(detected_pattern_name)
        ),
    )
    return True
