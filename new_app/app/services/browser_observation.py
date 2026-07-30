"""Turning a browser render into the ordinary detection/inference inputs.

The restricted browser strategy gives back rendered HTML plus any JSON the page
fetched. This service runs the *existing* PatternRegistry detection over both
and prefers a structured API: if the page's own JSON endpoint detects as an
event source, that is chosen over the rendered HTML, because a reusable API is
cheaper and more stable than re-rendering the page on every scheduled run.

Nothing site-specific happens here — detection is the same registry dispatch
used everywhere else, just fed a browser-observed response instead of an HTTP
one.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from app.extraction.browser import BrowserFetchStrategy, BrowserRenderResult
from app.extraction.detection import run_detection
from app.extraction.types import FetchResponse, PatternDetectionResult
from app.schemas.browser import BrowserPlan

# Patterns that operate on a fetched JSON body (as opposed to markup). When the
# page's own XHR/fetch reveals a response matching one of these, that response
# is a reusable structured HTTP endpoint and is preferred over re-rendering the
# page — which is the whole point of browser-assisted recovery. Kept as an
# explicit allow-list (not "any confident match") so a spurious HTML-oriented
# detector firing on a JSON body can never be chosen as the recurring source.
_STRUCTURED_API_PATTERNS = frozenset(
    {
        "embedded_json",
        "next_data",
        "nuxt_payload",
        "algolia_search",
        "wordpress_rest",
        "the_events_calendar",
        "livewhale_json",
    }
)


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
    blocked_reason: str | None = None
    rendered_response: FetchResponse | None = None
    api_responses: list[FetchResponse] = field(default_factory=list)
    chosen_response: FetchResponse | None = None
    detection: PatternDetectionResult | None = None
    chosen_source: str | None = None  # "structured_api" | "rendered_html"
    warnings: tuple[str, ...] = ()

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
        return BrowserObservation(blocked_reason=result.blocked_reason, warnings=result.warnings)

    from app.extraction.browser import observed_json_as_text

    rendered = _response(result.rendered_html, result.final_url, "text/html")
    api_responses = [
        _response(text, api_url, "application/json")
        for (api_url, _payload), text in zip(
            result.observed_json, observed_json_as_text(result.observed_json), strict=False
        )
    ]

    # Prefer a structured API: the best-detecting observed JSON response wins
    # over rendered HTML whenever it matches a JSON pattern.
    best_api: tuple[FetchResponse, PatternDetectionResult] | None = None
    for response in api_responses:
        detection = run_detection(response)
        if detection.pattern_name in _STRUCTURED_API_PATTERNS and (
            best_api is None or detection.confidence > best_api[1].confidence
        ):
            best_api = (response, detection)

    rendered_detection = run_detection(rendered)

    if best_api is not None:
        chosen, detection, source = best_api[0], best_api[1], "structured_api"
    else:
        chosen, detection, source = rendered, rendered_detection, "rendered_html"

    return BrowserObservation(
        rendered_response=rendered,
        api_responses=api_responses,
        chosen_response=chosen,
        detection=detection,
        chosen_source=source,
        warnings=result.warnings,
    )
