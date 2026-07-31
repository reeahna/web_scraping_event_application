"""Turning a browser render into the ordinary detection/inference inputs.

The restricted browser strategy gives back rendered HTML plus any JSON the page
fetched. A page fetches far more than its own event API — analytics, ad pixels,
social beacons, map tiles, third-party widgets. This service therefore does not
simply run detection over every response and take the best *detector* match:
that let an unknown-format first-party event API lose to a zero-result
rendered-HTML proposal, because only the HTML matched a registered pattern.

Instead it *classifies and scores* observed responses (app.extraction
.structured_candidates): third-party telemetry is filtered out, first-party
JSON is scored on event-likeness (record arrays, event fields, ownership), and a
qualifying structured event endpoint is preferred over rendered HTML — even when
no registered pattern recognises it yet, in which case recovery is told a
reusable pattern is needed rather than forcing generic HTML cards.

Nothing site-specific happens here: the target is only ever compared against its
own listing origin, and detection is the same registry dispatch used everywhere.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from app.extraction.browser import BrowserFetchStrategy, BrowserRenderResult
from app.extraction.detection import run_detection
from app.extraction.structured_candidates import (
    THIRD_PARTY_FUNCTIONAL,
    THIRD_PARTY_TELEMETRY,
    CandidateAnalysis,
    analyze_response,
)
from app.extraction.types import FetchResponse, PatternDetectionResult
from app.schemas.browser import BrowserPlan

# Patterns that operate on a fetched JSON body (as opposed to markup). A
# structured event candidate is only treated as *extractable now* when one of
# these recognises its body; otherwise it is a candidate that needs a new
# reusable pattern, surfaced rather than silently dropped.
_STRUCTURED_API_PATTERNS = frozenset(
    {
        "embedded_json",
        "next_data",
        "nuxt_payload",
        "algolia_search",
        "wordpress_rest",
        "the_events_calendar",
        "livewhale_json",
        "simpleview_events",
    }
)

# The single, authoritative outcome of an observation. Downstream recovery
# branches on this and nothing else, so an unextractable structured candidate
# can never be mistaken for a rendered-HTML selection.
OUTCOME_STRUCTURED_SELECTED = "structured_selected"
OUTCOME_STRUCTURED_PATTERN_NEEDED = "structured_pattern_needed"
OUTCOME_RENDERED_SELECTED = "rendered_selected"
OUTCOME_BLOCKED = "blocked"
OUTCOME_NO_SOURCE = "no_source"


def _response(body: str, final_url: str, content_type: str) -> FetchResponse:
    raw = body.encode("utf-8")
    return FetchResponse(
        request_url=final_url,
        final_url=final_url,
        status_code=200,
        headers={"content-type": content_type},
        content_type=content_type,
        body=raw,
        redirect_history=(),
        body_hash=hashlib.sha256(raw).hexdigest(),
        elapsed_seconds=0.0,
    )


@dataclass
class BrowserObservation:
    outcome: str = OUTCOME_NO_SOURCE
    blocked_reason: str | None = None
    rendered_response: FetchResponse | None = None
    api_responses: list[FetchResponse] = field(default_factory=list)
    # Set ONLY when a source is actually selected for extraction
    # (structured_selected / rendered_selected). For structured_pattern_needed
    # and no_source it is None, so a caller cannot accidentally extract from it.
    chosen_response: FetchResponse | None = None
    detection: PatternDetectionResult | None = None
    chosen_source: str | None = None  # "structured_api" | "rendered_html" | None
    warnings: tuple[str, ...] = ()
    # Classified, scored observed responses.
    candidate_endpoints: list[CandidateAnalysis] = field(default_factory=list)
    ignored_endpoints: list[CandidateAnalysis] = field(default_factory=list)
    other_first_party: list[CandidateAnalysis] = field(default_factory=list)
    selected_endpoint: str | None = None
    selection_reason: str | None = None
    # Bounded, redacted request metadata for the selected/preferred endpoint,
    # for safe recurring HTTP replay by the proposer.
    selected_request_metadata: dict | None = None
    rejected_candidates: list[dict] = field(default_factory=list)
    # A first-party event endpoint scored as a candidate but no registered
    # pattern can extract it — recovery should route to review, not force
    # rendered HTML.
    unextracted_candidate: CandidateAnalysis | None = None
    new_pattern_needed: bool = False

    @property
    def usable(self) -> bool:
        return self.detection is not None and self.detection.pattern_name is not None


async def render_and_observe(
    url: str,
    *,
    plan: BrowserPlan | None = None,
    strategy: BrowserFetchStrategy | None = None,
) -> BrowserObservation:
    strategy = strategy or BrowserFetchStrategy()
    result: BrowserRenderResult = await strategy.render(url, plan)

    if result.blocked_reason is not None:
        return BrowserObservation(
            outcome=OUTCOME_BLOCKED,
            blocked_reason=result.blocked_reason,
            warnings=result.warnings,
        )

    from app.extraction.browser import observed_json_as_text

    rendered = _response(result.rendered_html, result.final_url, "text/html")

    # Classify + score every observed JSON response against the *listing* origin
    # (not the render's final_url), then keep each analysis paired with a
    # FetchResponse so detectors can run on the actual body.
    analyses: list[tuple[CandidateAnalysis, FetchResponse]] = []
    for (api_url, payload), text in zip(
        result.observed_json, observed_json_as_text(result.observed_json), strict=False
    ):
        analysis = analyze_response(
            url=api_url, payload=payload, listing_url=url, content_type="application/json",
            raw_text=text, request_meta=result.observed_requests.get(api_url),
        )
        analyses.append((analysis, _response(text, api_url, "application/json")))

    candidate_endpoints = [a for a, _ in analyses if a.is_event_candidate]
    candidate_endpoints.sort(key=lambda a: a.event_likeness_score, reverse=True)
    ignored_endpoints = [
        a for a, _ in analyses
        if a.classification in (THIRD_PARTY_FUNCTIONAL, THIRD_PARTY_TELEMETRY)
    ]
    other_first_party = [
        a for a, _ in analyses
        if a not in candidate_endpoints and a not in ignored_endpoints
    ]

    warnings: list[str] = list(result.warnings)
    rejected: list[dict] = []

    # Prefer the highest-scoring first-party event candidate that a registered
    # pattern can actually extract.
    best_extractable: tuple[FetchResponse, PatternDetectionResult, CandidateAnalysis] | None = None
    for analysis in candidate_endpoints:
        response = next(resp for a, resp in analyses if a is analysis)
        detection = run_detection(response)
        if detection.pattern_name in _STRUCTURED_API_PATTERNS:
            best_extractable = (response, detection, analysis)
            break
        rejected.append(
            {
                "url": analysis.sanitized_url,
                "score": round(analysis.event_likeness_score, 4),
                "reason": "no registered pattern can extract this response shape",
            }
        )

    api_responses = [resp for _, resp in analyses]

    # 1. Extractable first-party structured event endpoint — the best case.
    if best_extractable is not None:
        chosen, detection, analysis = best_extractable
        return BrowserObservation(
            outcome=OUTCOME_STRUCTURED_SELECTED,
            rendered_response=rendered,
            api_responses=api_responses,
            chosen_response=chosen,
            detection=detection,
            chosen_source="structured_api",
            warnings=tuple(warnings),
            candidate_endpoints=candidate_endpoints,
            ignored_endpoints=ignored_endpoints,
            other_first_party=other_first_party,
            selected_endpoint=analysis.sanitized_url,
            selection_reason=(
                f"first-party structured event endpoint recognised by "
                f"'{detection.pattern_name}' (event-likeness "
                f"{analysis.event_likeness_score:.2f})"
            ),
            selected_request_metadata=analysis.request_metadata,
            rejected_candidates=rejected,
        )

    # 2. A qualifying first-party event endpoint exists but no registered
    # pattern can extract it. This is NOT a rendered-HTML selection: a reusable
    # pattern is needed. chosen_response/detection stay None so recovery cannot
    # extract or propose from it, and rendered HTML is never selected here.
    if candidate_endpoints:
        top = candidate_endpoints[0]
        warnings.append(
            "a first-party event-like endpoint was observed but no registered pattern can "
            "extract it - a reusable pattern is needed"
        )
        return BrowserObservation(
            outcome=OUTCOME_STRUCTURED_PATTERN_NEEDED,
            rendered_response=rendered,  # retained for diagnostics only
            api_responses=api_responses,
            chosen_response=None,
            detection=None,
            chosen_source=None,
            warnings=tuple(warnings),
            candidate_endpoints=candidate_endpoints,
            ignored_endpoints=ignored_endpoints,
            other_first_party=other_first_party,
            selected_endpoint=top.sanitized_url,
            selection_reason=(
                f"first-party structured event endpoint <{top.sanitized_url}> is preferred "
                f"(event-likeness {top.event_likeness_score:.2f}) but no registered pattern "
                f"supports its response shape yet"
            ),
            selected_request_metadata=top.request_metadata,
            rejected_candidates=rejected,
            unextracted_candidate=top,
            new_pattern_needed=True,
        )

    # 3. No structured candidate at all — only now may rendered HTML be
    # considered, and it still goes through the ordinary proposal + preview.
    rendered_detection = run_detection(rendered)
    outcome = (
        OUTCOME_RENDERED_SELECTED if rendered_detection.pattern_name else OUTCOME_NO_SOURCE
    )
    selection_reason = (
        f"no structured candidate observed; rendered HTML detected as "
        f"'{rendered_detection.pattern_name}'"
        if rendered_detection.pattern_name
        else "no structured candidate and rendered HTML matched no pattern"
    )
    return BrowserObservation(
        outcome=outcome,
        rendered_response=rendered,
        api_responses=api_responses,
        chosen_response=rendered,
        detection=rendered_detection,
        chosen_source="rendered_html",
        warnings=tuple(warnings),
        candidate_endpoints=candidate_endpoints,
        ignored_endpoints=ignored_endpoints,
        other_first_party=other_first_party,
        selected_endpoint=None,
        selection_reason=selection_reason,
        rejected_candidates=rejected,
    )
