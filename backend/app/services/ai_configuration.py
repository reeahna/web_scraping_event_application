"""AI-assisted configuration fallback.

Used only when deterministic inference could not produce a reliable draft. The
sequence reuses existing production services end to end:

    detect -> build bounded evidence -> provider suggests -> validate the
    suggestion -> save it as a DRAFT (origin ai_suggested) -> preview
    deterministically -> Phase 8D policy evaluates it

The AI never approves, activates, publishes, or persists an Event row. Because
the seeded policy denies AI-origin approval, a suggestion becomes at most a
previewed draft awaiting a human — which is exactly the intended safety
default. Recurring extraction after any later approval is deterministic and
does not call the provider.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.auto_onboarding import ORIGIN_AI_SUGGESTED
from app.extraction.registry import REGISTRY
from app.models.extraction_run import ExtractionRun
from app.models.website import Website
from app.repositories.auto_onboarding import (
    policy_city_ids,
    policy_role_ids,
    resolve_policy,
)
from app.services import extraction_runs
from app.services.ai.evidence import build_evidence
from app.services.ai.provider import (
    AIProviderError,
    AIProviderUnavailable,
    get_ai_provider,
)
from app.services.ai.suggestion import validate_suggestion
from app.services.ai.types import AISuggestionRequest
from app.services.audit import record_audit
from app.services.auto_onboarding_decision import (
    AutoOnboardingDecisionService,
    DecisionContext,
    snapshot_policy,
)
from app.services.auto_onboarding_execution import execute_decision
from app.services.auto_onboarding_persistence import record_decision
from app.services.website_configuration import save_draft_configuration


@dataclass(frozen=True)
class AIConfigurationOutcome:
    status: str  # disabled | no_suggestion | invalid | drafted | error
    provider: str
    detail: str | None = None
    errors: tuple[str, ...] = ()
    configuration_version: int | None = None
    preview_status: str | None = None
    events_valid: int = 0
    decision_id: int | None = None


async def request_ai_configuration(
    db: Session,
    website: Website,
    *,
    actor_id: int | None = None,
    correlation_id: str | None = None,
) -> AIConfigurationOutcome:
    provider = get_ai_provider()
    if not provider.available():
        return AIConfigurationOutcome(status="disabled", provider=provider.name,
                                      detail="the AI configuration assistant is disabled")

    # Fresh detection gives the evidence builder a current document and the
    # detector's own findings; it also records an ordinary detection run.
    detection_outcome = await extraction_runs.run_detection_detailed(
        db, website, correlation_id=correlation_id
    )
    if detection_outcome.response.blocked_reason is not None:
        return AIConfigurationOutcome(
            status="error",
            provider=provider.name,
            detail=f"the source could not be fetched: {detection_outcome.response.blocked_reason}",
        )

    policy = resolve_policy(db, city_id=website.city_id)
    allowed = frozenset(policy.allowed_pattern_names or ()) if policy else frozenset()
    evidence = build_evidence(
        response=detection_outcome.response,
        detection=detection_outcome.detection,
        listing_url=detection_outcome.listing_url,
        validation_failures=tuple(
            (website.proposed_pattern or {}).get("inference", {}).get("blocking_reasons") or ()
        ),
        allowed_pattern_names=tuple(sorted(allowed)) or tuple(REGISTRY.names()),
    )

    try:
        result = provider.suggest(
            AISuggestionRequest(
                website_id=website.id, evidence=evidence, correlation_id=correlation_id
            )
        )
    except AIProviderUnavailable as exc:
        return AIConfigurationOutcome(status="disabled", provider=provider.name, detail=str(exc))
    except AIProviderError as exc:
        record_audit(
            db,
            actor_id=actor_id,
            action="ai_configuration_suggestion_failed",
            entity_type="website",
            entity_id=website.id,
            after={"provider": provider.name, "error": str(exc)[:300]},
            correlation_id=correlation_id,
        )
        return AIConfigurationOutcome(status="error", provider=provider.name, detail=str(exc))

    if not result.ok or result.suggestion is None:
        return AIConfigurationOutcome(
            status="no_suggestion", provider=provider.name,
            detail=result.error or "the provider returned no suggestion",
        )

    validation = validate_suggestion(
        result.suggestion,
        allowed_pattern_names=allowed,
        registered_patterns=frozenset(REGISTRY.names()),
    )
    if not validation.ok:
        record_audit(
            db,
            actor_id=actor_id,
            action="ai_configuration_suggestion_rejected",
            entity_type="website",
            entity_id=website.id,
            after={"provider": provider.name, "errors": list(validation.errors[:10])},
            correlation_id=correlation_id,
        )
        return AIConfigurationOutcome(
            status="invalid", provider=provider.name,
            detail="the suggestion did not pass validation",
            errors=validation.errors,
        )

    # Draft only. Never approved, never activated by this path.
    save_draft_configuration(db, website, validation.configuration)
    website.configuration_origin = ORIGIN_AI_SUGGESTED
    db.commit()

    preview = await extraction_runs.preview_extraction_detailed(
        db, website, correlation_id=correlation_id
    )
    preview_run = (
        db.get(ExtractionRun, preview.result.run_id) if preview.result.run_id else None
    )

    snapshot = (
        snapshot_policy(
            policy,
            city_ids=policy_city_ids(db, policy.id),
            role_ids=policy_role_ids(db, policy.id),
        )
        if policy is not None
        else None
    )
    context = DecisionContext(
        policy=snapshot,
        website_id=website.id,
        website_is_archived=website.archived_at is not None,
        website_onboarding_status=website.onboarding_status,
        city_id=website.city_id,
        city_is_active=bool(website.city and website.city.is_active),
        detected_pattern=validation.configuration.pattern_name,
        detector_confidence=detection_outcome.detection.confidence,
        detection_status=detection_outcome.result.status,
        browser_required=detection_outcome.detection.browser_required,
        blocked=preview.result.status == "blocked",
        registered_patterns=frozenset(REGISTRY.names()),
        configuration=validation.configuration,
        configuration_origin=ORIGIN_AI_SUGGESTED,
        configuration_version=website.configuration_version,
        preview_run_id=preview.result.run_id,
        preview_status=preview.result.status,
        preview_configuration_version=(
            preview_run.configuration_version if preview_run else None
        ),
        quality=preview.quality,
        warnings=tuple(preview.result.warnings),
        missing_required_fields=(),
        detail_enrichment_used=bool(preview.quality and preview.quality.detail_fetch_used),
        submitted_by_user_id=actor_id,
    )
    decision = record_decision(
        db,
        AutoOnboardingDecisionService().evaluate(context),
        website=website,
        submitted_by_user_id=actor_id,
        correlation_id=correlation_id,
    )
    # Executes only what the policy permits — which, for an AI-origin
    # configuration under the default policy, is nothing.
    execute_decision(db, decision, website=website, correlation_id=correlation_id)

    record_audit(
        db,
        actor_id=actor_id,
        action="ai_configuration_suggestion_drafted",
        entity_type="website",
        entity_id=website.id,
        after={
            "provider": provider.name,
            "pattern": validation.configuration.pattern_name,
            "configuration_version": website.configuration_version,
            "preview_status": preview.result.status,
            "events_valid": preview.result.events_valid,
            "decision_id": decision.id,
        },
        correlation_id=correlation_id,
    )
    return AIConfigurationOutcome(
        status="drafted",
        provider=provider.name,
        configuration_version=website.configuration_version,
        preview_status=preview.result.status,
        events_valid=preview.result.events_valid,
        decision_id=decision.id,
    )
