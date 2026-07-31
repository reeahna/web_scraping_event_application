"""Configuration proposer for the Simpleview events API.

Builds a complete draft `SiteConfiguration` from a confirmed Simpleview
response (records at `docs.docs`). It measures field coverage over the actual
sample records, prefers `recid` as the stable id, and — when the browser
observation captured the request — replays the *confirmed* method (GET with
safe query params, or POST with a bounded JSON body). When the method or
pagination could not be confirmed it proposes single-response GET extraction
and says so in warnings rather than guessing.

Nothing here is site-specific: the endpoint comes from the detection result,
every path is a default the admin can override, and no hostname/body value is
ever hardcoded.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.extraction.inference.base import DEFAULT_REQUIRED_FIELDS, failed_proposal
from app.extraction.inference.types import (
    ConfigurationProposal,
    FieldSelectorCandidate,
    ProposalContext,
)
from app.extraction.patterns.simpleview_events import _DEFAULT_PATHS, EVENTS_ROOT_DEFAULT
from app.extraction.selectors import resolve_json_path
from app.schemas.extraction import SiteConfiguration

NAME = "simpleview_events"

# Bounded ceilings for a replayed POST body captured from the browser.
_MAX_REPLAY_BODY_BYTES = 4_000

# Required-field name mapping: the extractor's raw key -> the engine field the
# required-fields check uses.
_REQUIRED_SOURCE = {
    "title": "title",
    "start_date": "start_datetime",
    "canonical_url": "canonical_url",
}


def _records(payload: Any) -> list[dict]:
    resolved = resolve_json_path(payload, EVENTS_ROOT_DEFAULT).value
    return [r for r in resolved if isinstance(r, dict)] if isinstance(resolved, list) else []


def _coverage(records: list[dict], path: str) -> tuple[float, list[str]]:
    if not records:
        return 1.0, []
    present, samples = 0, []
    for record in records:
        value = resolve_json_path(record, path).value
        if value not in (None, "", [], {}):
            present += 1
            if len(samples) < 3:
                samples.append(str(value)[:80])
    return present / len(records), samples


def _replay_fetch(request_metadata: dict | None) -> tuple[dict, list[str]]:
    """A closed, validated fetch dict from bounded observed request metadata.
    Only the method, safe query params, and a bounded JSON body are carried —
    never cookies, auth, tokens, or arbitrary headers."""
    warnings: list[str] = []
    if not request_metadata:
        warnings.append("request method not observed; defaulting to GET single-response replay")
        return {}, warnings

    method = str(request_metadata.get("method", "GET")).upper()
    if method == "POST":
        body = request_metadata.get("request_body")
        if isinstance(body, str) and 0 < len(body.encode("utf-8")) <= _MAX_REPLAY_BODY_BYTES:
            try:
                parsed = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                return {"method": "POST", "json_body": parsed}, warnings
        warnings.append(
            "observed a POST request but its body could not be safely templated; "
            "confirm the request body manually before approval"
        )
        return {}, warnings

    query = request_metadata.get("query_params")
    if isinstance(query, dict) and query:
        safe = {str(k): str(v) for k, v in query.items() if isinstance(k, str)}
        return {"query_params": safe}, warnings
    return {}, warnings


class SimpleviewEventsProposer:
    pattern_name = NAME

    def propose(self, context: ProposalContext) -> ConfigurationProposal:
        try:
            payload = json.loads(context.response.text)
        except (json.JSONDecodeError, ValueError):
            return failed_proposal("the Simpleview response was not valid JSON")

        records = _records(payload)
        if not records:
            return failed_proposal("no Simpleview event records were found at docs.docs")

        endpoint = None
        for candidate in context.detection.discovered_endpoints:
            if candidate:
                endpoint = candidate
                break
        endpoint = endpoint or context.response.final_url
        # Never persist the observed query string — it may carry a token. The
        # bare endpoint is stored; any required filter params are surfaced as a
        # warning for the administrator to configure deliberately.
        parts = urlsplit(endpoint)
        observed_query_names = sorted({p.split("=", 1)[0] for p in parts.query.split("&") if p})
        endpoint = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

        # Field coverage over the real sample records.
        field_candidates: list[FieldSelectorCandidate] = []
        missing: list[str] = []
        min_cov = getattr(context.policy, "min_field_coverage", 0.5)
        for engine_field, source_key in _REQUIRED_SOURCE.items():
            path = _DEFAULT_PATHS.get(source_key, source_key)
            coverage, samples = _coverage(records, path)
            accepted = coverage >= min_cov
            field_candidates.append(
                FieldSelectorCandidate(
                    field=engine_field, kind="json_path", selector=path, attribute=None,
                    confidence=round(0.6 + 0.35 * coverage, 4), coverage=coverage,
                    parse_success_rate=None, evidence=(f"docs.docs[].{path}",),
                    sample_values=tuple(samples), warnings=(), alternatives=(), accepted=accepted,
                )
            )
            if not accepted:
                missing.append(engine_field)

        replay_fetch, replay_warnings = _replay_fetch(getattr(context, "request_metadata", None))

        try:
            configuration = SiteConfiguration(
                pattern_name=NAME,
                api_endpoint=endpoint,
                timezone=context.fallback_timezone,
                fetch=replay_fetch,
                # Explicit so the record-array root is visible and overridable.
                json_paths={"events_root": EVENTS_ROOT_DEFAULT},
                pagination={
                    "strategy": "none",
                    "max_pages": context.policy.max_pages,
                    "max_events": context.policy.max_events,
                },
                max_detail_fetches=0,
                required_fields=list(DEFAULT_REQUIRED_FIELDS),
            )
        except ValueError as exc:
            return failed_proposal(f"proposed Simpleview configuration failed validation: {exc}")

        id_field = next(
            (k for k in ("recid", "_id", "id") if records[0].get(k) not in (None, "")), None
        )
        notes = (
            f"Simpleview events API at {endpoint}",
            f"record array: {EVENTS_ROOT_DEFAULT}",
            f"stable id field: {id_field or 'none observed'}",
            "field mappings are the pattern defaults, editable under 'JSON paths'",
        )
        warnings = (
            "pagination not confirmed from observation; extracting a single response. "
            "Configure query-param pagination once the mechanism is confirmed.",
            *replay_warnings,
            *(
                (f"observed query parameters {observed_query_names} were not persisted "
                 "(they may carry tokens); configure required filters manually.",)
                if observed_query_names
                else ()
            ),
        )
        return ConfigurationProposal(
            configuration=configuration,
            field_candidates=tuple(field_candidates),
            confidence=round(min(0.9, 0.5 + 0.4 * context.detection.confidence), 4),
            missing_required_fields=tuple(missing),
            warnings=warnings,
            notes=notes,
        )


def simpleview_events_proposer() -> SimpleviewEventsProposer:
    return SimpleviewEventsProposer()
