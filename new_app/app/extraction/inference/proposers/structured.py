"""Configuration proposers for the structured patterns.

A structured pattern already knows the shape of the response it will get, so
these proposers don't have to infer field mappings from markup at all — they
propose the endpoint, the matching pagination strategy, and the pattern's own
documented response schema as the field mapping, leaving
`SiteConfiguration.json_paths` empty so every mapping stays overridable by an
administrator without the default being lost.

Each proposer reads the endpoint from `detection.discovered_endpoints`, which
the detectors derive from the page's own discovery links or route
conventions. None of them looks at the hostname.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin

from app.extraction.inference.base import DEFAULT_REQUIRED_FIELDS, failed_proposal
from app.extraction.inference.policy import AutoOnboardingPolicy
from app.extraction.inference.types import (
    ConfigurationProposal,
    FieldSelectorCandidate,
    ProposalContext,
)
from app.extraction.patterns.jsonld import _DEFAULT_PATHS as JSONLD_PATHS
from app.extraction.patterns.livewhale_json import _DEFAULT_PATHS as LIVEWHALE_PATHS
from app.extraction.patterns.the_events_calendar import _DEFAULT_PATHS as TRIBE_PATHS
from app.extraction.patterns.wordpress_rest import _DEFAULT_PATHS as WORDPRESS_PATHS
from app.schemas.extraction import SiteConfiguration

# Fields worth surfacing to an administrator as "this is where each value
# will come from". The patterns map more than this into `raw` for provenance;
# these are the ones with a persisted Event column and a review value.
_REPORTED_FIELDS: tuple[str, ...] = (
    "title",
    "canonical_url",
    "start_datetime",
    "end_datetime",
    "venue",
    "address",
    "image",
    "description",
    "source_category",
    "external_source_id",
)

_REQUIRED_JSON_FIELDS: tuple[str, ...] = ("title", "canonical_url", "start_datetime")


def _payload_records(text: str) -> list[dict[str, Any]] | None:
    """The listing response is only sometimes the API response itself (it is
    when the admin pasted the API URL directly). When it is, coverage is
    measured against real records; when it isn't, the schema is still
    proposed but reported as unmeasured."""
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(payload, dict):
        events = payload.get("events")
        if isinstance(events, list):
            return [e for e in events if isinstance(e, dict)]
        return [payload]
    if isinstance(payload, list):
        return [e for e in payload if isinstance(e, dict)]
    return None


def _resolve_path(record: dict[str, Any], path: str) -> Any:
    from app.extraction.selectors import resolve_json_path

    return resolve_json_path(record, path).value


def _schema_candidates(
    paths: dict[str, str], records: list[dict[str, Any]] | None, policy: AutoOnboardingPolicy
) -> tuple[tuple[FieldSelectorCandidate, ...], tuple[str, ...]]:
    candidates: list[FieldSelectorCandidate] = []
    missing: list[str] = []
    for field_name in _REPORTED_FIELDS:
        path = paths.get(field_name)
        if path is None:
            continue
        if records:
            values = [_resolve_path(record, path) for record in records]
            present = [v for v in values if v not in (None, "", False)]
            coverage = len(present) / len(values)
            samples = tuple(str(v)[:120] for v in present[: policy.max_sample_values_recorded])
            evidence = (
                "pattern's documented response schema",
                f"observed in {len(present)}/{len(values)} records in this response",
            )
        else:
            coverage = 1.0
            samples = ()
            evidence = (
                "pattern's documented response schema",
                "endpoint not fetched at proposal time — coverage confirmed by the "
                "automatic preview that follows",
            )
        accepted = coverage >= policy.min_field_coverage
        if not accepted and field_name in _REQUIRED_JSON_FIELDS:
            missing.append("start_date" if field_name == "start_datetime" else field_name)
        candidates.append(
            FieldSelectorCandidate(
                field=field_name,
                kind="json_path",
                selector=path,
                attribute=None,
                confidence=round(0.6 + 0.35 * coverage, 4),
                coverage=coverage,
                parse_success_rate=None,
                evidence=evidence,
                sample_values=samples,
                warnings=() if accepted else ("absent_from_sampled_records",),
                alternatives=(),
                accepted=accepted,
            )
        )
    return tuple(candidates), tuple(missing)


@dataclass(frozen=True)
class _StructuredSpec:
    pattern_name: str
    paths: dict[str, str]
    pagination_strategy: str
    page_param: str | None
    endpoint_note: str
    # Route appended to a discovered API root when the detector could only
    # discover the root. A deterministic route convention, not a guess about
    # which site this is.
    derived_route: str | None = None


class _StructuredProposer:
    """Shared body for every JSON-endpoint pattern. The only thing that
    varies between them is the `_StructuredSpec` data."""

    def __init__(self, spec: _StructuredSpec) -> None:
        self._spec = spec
        self.pattern_name = spec.pattern_name

    def _endpoint(self, context: ProposalContext) -> str | None:
        for endpoint in context.detection.discovered_endpoints:
            if endpoint:
                if self._spec.derived_route and endpoint.rstrip("/").endswith("wp-json"):
                    return urljoin(endpoint.rstrip("/") + "/", self._spec.derived_route)
                return endpoint
        # The listing URL itself is a usable endpoint when the response we
        # already hold is the API payload.
        if _payload_records(context.response.text) is not None:
            return context.response.final_url
        return None

    def propose(self, context: ProposalContext) -> ConfigurationProposal:
        spec = self._spec
        endpoint = self._endpoint(context)
        if not endpoint:
            return failed_proposal(
                f"{spec.pattern_name} was detected but no API endpoint could be discovered"
            )

        records = _payload_records(context.response.text)
        # Only treat the body as sample records when it really is this
        # endpoint's payload, not an unrelated HTML page.
        if context.response.final_url != endpoint:
            records = None

        candidates, missing = _schema_candidates(spec.paths, records, context.policy)

        try:
            configuration = SiteConfiguration(
                pattern_name=spec.pattern_name,
                api_endpoint=endpoint,
                timezone=context.fallback_timezone,
                json_paths={},
                pagination={
                    "strategy": spec.pagination_strategy,
                    "page_param": spec.page_param,
                    "max_pages": context.policy.max_pages,
                    "max_events": context.policy.max_events,
                },
                max_detail_fetches=0,
                required_fields=list(DEFAULT_REQUIRED_FIELDS),
            )
        except ValueError as exc:
            return failed_proposal(f"proposed configuration failed validation: {exc}")

        return ConfigurationProposal(
            configuration=configuration,
            field_candidates=candidates,
            confidence=round(min(0.9, 0.5 + 0.4 * context.detection.confidence), 4),
            missing_required_fields=missing,
            notes=(
                spec.endpoint_note,
                f"endpoint: {endpoint}",
                f"pagination: {spec.pagination_strategy}",
                "field mappings come from the pattern's default schema and stay editable "
                "under 'JSON paths' in advanced configuration",
            ),
        )


class JsonLdEventProposer:
    """JSON-LD is embedded in the listing page itself, so unlike the API
    patterns this one can measure its own field coverage directly against the
    nodes already present in the response."""

    pattern_name = "json_ld_event"

    def propose(self, context: ProposalContext) -> ConfigurationProposal:
        from app.extraction.patterns.jsonld import _find_jsonld_nodes, _is_event_type

        nodes = [n for n in _find_jsonld_nodes(context.response.text) if _is_event_type(n)]
        if not nodes:
            return failed_proposal("no schema.org Event nodes were found in this page")

        candidates, missing = _schema_candidates(JSONLD_PATHS, nodes, context.policy)
        notes = [f"{len(nodes)} schema.org Event node(s) in the listing page"]

        # Canonical-URL fallbacks are opt-in flags in the schema precisely so
        # they are never applied silently; they are only proposed when the
        # nodes themselves show the URL is otherwise unavailable.
        has_url = any(node.get("url") for node in nodes)
        has_offers_url = any(
            isinstance(node.get("offers"), dict) and node["offers"].get("url") for node in nodes
        )
        allow_offers = not has_url and has_offers_url
        allow_page_url = not has_url and not has_offers_url and len(nodes) == 1
        if allow_offers:
            notes.append("no node carries `url`; proposing offers.url as the event URL")
        if allow_page_url:
            notes.append("single event node with no `url`; proposing the page URL as canonical")
        if not has_url and (allow_offers or allow_page_url):
            missing = tuple(m for m in missing if m != "canonical_url")

        try:
            configuration = SiteConfiguration(
                pattern_name=self.pattern_name,
                listing_url=context.listing_url,
                timezone=context.fallback_timezone,
                json_paths={},
                pagination={
                    "strategy": "none",
                    "max_pages": context.policy.max_pages,
                    "max_events": context.policy.max_events,
                },
                max_detail_fetches=0,
                required_fields=list(DEFAULT_REQUIRED_FIELDS),
                allow_page_url_as_canonical_fallback=allow_page_url,
                allow_offers_url_as_event_url=allow_offers,
            )
        except ValueError as exc:
            return failed_proposal(f"proposed configuration failed validation: {exc}")

        return ConfigurationProposal(
            configuration=configuration,
            field_candidates=candidates,
            confidence=round(min(0.9, 0.5 + 0.4 * context.detection.confidence), 4),
            missing_required_fields=missing,
            notes=tuple(notes),
        )


def wordpress_rest_proposer() -> _StructuredProposer:
    return _StructuredProposer(
        _StructuredSpec(
            pattern_name="wordpress_rest",
            paths=WORDPRESS_PATHS,
            pagination_strategy="wordpress",
            page_param="page",
            derived_route="wp/v2/posts",
            endpoint_note=(
                "WordPress REST endpoint derived from the page's own wp-json discovery link"
            ),
        )
    )


def the_events_calendar_proposer() -> _StructuredProposer:
    return _StructuredProposer(
        _StructuredSpec(
            pattern_name="the_events_calendar",
            paths=TRIBE_PATHS,
            pagination_strategy="tribe_rest",
            page_param=None,
            endpoint_note="The Events Calendar REST route discovered on the page",
        )
    )


def livewhale_proposer() -> _StructuredProposer:
    return _StructuredProposer(
        _StructuredSpec(
            pattern_name="livewhale_json",
            paths=LIVEWHALE_PATHS,
            pagination_strategy="livewhale_offset",
            page_param="offset",
            endpoint_note="LiveWhale calendar API route discovered on the page",
        )
    )
