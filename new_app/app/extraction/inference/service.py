"""ConfigurationInferenceService: detection result + response -> a complete,
validated draft configuration, or a reasoned refusal.

Provider-independent by construction. The service knows three things: how to
classify a response that can't be configured at all (blocked / browser
required / no pattern), how to look a proposer up in the registry, and how to
turn a proposal into an outcome. It contains no per-pattern branch and no
per-site branch — adding a pattern means registering a proposer, and changes
nothing here.
"""

from __future__ import annotations

from app.extraction.inference.policy import (
    BLOCKED,
    BROWSER_REQUIRED,
    DEFAULT_POLICY,
    FAILED,
    NEEDS_REVIEW,
    READY_FOR_APPROVAL,
    UNSUPPORTED,
    AutoOnboardingPolicy,
)
from app.extraction.inference.types import (
    ConfigurationProposal,
    InferenceResult,
    ProposalContext,
)
from app.extraction.registry import PatternRegistry, UnsupportedPatternError
from app.extraction.types import FetchResponse, PatternDetectionResult


def _terminal(
    outcome: str,
    detection: PatternDetectionResult,
    *,
    error: str | None = None,
    notes: tuple[str, ...] = (),
) -> InferenceResult:
    return InferenceResult(
        outcome=outcome,
        pattern_name=detection.pattern_name,
        detection_confidence=detection.confidence,
        proposal_confidence=0.0,
        configuration=None,
        field_candidates=(),
        date_format_candidates=(),
        missing_required_fields=(),
        warnings=tuple(detection.warnings),
        notes=notes,
        error=error,
    )


class ConfigurationInferenceService:
    def __init__(
        self,
        registry: PatternRegistry,
        *,
        policy: AutoOnboardingPolicy = DEFAULT_POLICY,
    ) -> None:
        self._registry = registry
        self._policy = policy

    @property
    def policy(self) -> AutoOnboardingPolicy:
        return self._policy

    def build_context(
        self,
        *,
        response: FetchResponse,
        detection: PatternDetectionResult,
        listing_url: str,
        fallback_timezone: str | None,
        detail_documents: dict[str, str] | None = None,
    ) -> ProposalContext:
        return ProposalContext(
            response=response,
            detection=detection,
            listing_url=listing_url,
            fallback_timezone=fallback_timezone,
            policy=self._policy,
            detail_documents=dict(detail_documents or {}),
        )

    def propose(self, context: ProposalContext) -> ConfigurationProposal | InferenceResult:
        """Runs the registered proposer for the detected pattern. Returns a
        terminal InferenceResult when the response can't be configured at
        all; otherwise the raw proposal, which may still be asking for a
        detail-page document (`detail_probe_url`)."""
        detection = context.detection
        if context.response.blocked_reason is not None:
            return _terminal(BLOCKED, detection, error=str(context.response.blocked_reason))
        if detection.browser_required:
            return _terminal(
                BROWSER_REQUIRED,
                detection,
                error="this site requires browser rendering, which is not supported",
            )
        if detection.pattern_name is None:
            return _terminal(
                UNSUPPORTED, detection, error="no extraction pattern matched with confidence"
            )

        try:
            registration = self._registry.get(detection.pattern_name)
        except UnsupportedPatternError as exc:
            return _terminal(UNSUPPORTED, detection, error=str(exc))

        proposer = registration.proposer
        if proposer is None:
            return _terminal(
                NEEDS_REVIEW,
                detection,
                error=(
                    f"pattern '{detection.pattern_name}' has no configuration proposer; "
                    "configure it manually"
                ),
            )
        return proposer.propose(context)

    def finalize(
        self, proposal: ConfigurationProposal, detection: PatternDetectionResult
    ) -> InferenceResult:
        """Turns a completed proposal into a provisional outcome. The outcome
        is only ever `ready_for_approval` when nothing in the proposal itself
        blocks approval — the preview that follows can still demote it."""
        if proposal.error is not None or proposal.configuration is None:
            return InferenceResult(
                outcome=NEEDS_REVIEW if proposal.field_candidates else FAILED,
                pattern_name=detection.pattern_name,
                detection_confidence=detection.confidence,
                proposal_confidence=proposal.confidence,
                configuration=None,
                field_candidates=proposal.field_candidates,
                date_format_candidates=proposal.date_format_candidates,
                missing_required_fields=proposal.missing_required_fields,
                warnings=proposal.warnings,
                notes=proposal.notes,
                error=proposal.error or "no configuration could be proposed",
            )

        outcome = NEEDS_REVIEW if proposal.missing_required_fields else READY_FOR_APPROVAL
        return InferenceResult(
            outcome=outcome,
            pattern_name=detection.pattern_name,
            detection_confidence=detection.confidence,
            proposal_confidence=proposal.confidence,
            configuration=proposal.configuration,
            field_candidates=proposal.field_candidates,
            date_format_candidates=proposal.date_format_candidates,
            missing_required_fields=proposal.missing_required_fields,
            warnings=proposal.warnings,
            notes=proposal.notes,
            error=None,
        )
