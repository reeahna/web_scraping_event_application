"""Configuration proposer for paged JSON event APIs (`paged_json_events`).

Builds a complete draft from a confirmed paged response: the record-array root
and every field path are inferred from the real sample records (and stored in
`json_paths`, where an administrator can override them), and pagination follows
the response's own page metadata over plain HTTP.

Nothing here is site-specific: the endpoint comes from the detection result and
no hostname is ever referenced.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit, urlunsplit

from app.extraction.inference.base import DEFAULT_REQUIRED_FIELDS, failed_proposal
from app.extraction.inference.types import (
    ConfigurationProposal,
    FieldSelectorCandidate,
    ProposalContext,
)
from app.extraction.patterns.paged_json_events import (
    NAME,
    infer_field_paths,
    record_root,
    records_at,
)
from app.extraction.selectors import resolve_json_path
from app.schemas.extraction import SiteConfiguration

# Requested page size when the API is a Spring Data page (it accepts `size`).
# Fewer, larger pages are kinder to the source than many 10-record pages.
SPRING_PAGE_SIZE = 50

# Engine field -> the extractor's raw key, for the required-fields check.
_REQUIRED_SOURCE = {
    "title": "title",
    "start_date": "start_datetime",
    "canonical_url": "canonical_url",
}


def _coverage(records: list[dict], path: str | None) -> tuple[float, list[str]]:
    if not records or not path:
        return 0.0, []
    present, samples = 0, []
    for record in records:
        value = resolve_json_path(record, path).value
        if value not in (None, "", [], {}):
            present += 1
            if len(samples) < 3:
                samples.append(str(value)[:80])
    return present / len(records), samples


class PagedJsonEventsProposer:
    pattern_name = NAME

    def propose(self, context: ProposalContext) -> ConfigurationProposal:
        try:
            payload = json.loads(context.response.text)
        except (json.JSONDecodeError, ValueError):
            return failed_proposal("the paged JSON response was not valid JSON")
        root = record_root(payload)
        records = records_at(payload, root) if root else []
        if not root or not records:
            return failed_proposal("no records were found in the paged JSON response")

        paths = infer_field_paths(records)

        endpoint = next(
            (e for e in context.detection.discovered_endpoints if e), context.response.final_url
        )
        # Never persist the observed query string — it may carry a token or a
        # page position. Required filters are surfaced as a warning instead.
        parts = urlsplit(endpoint)
        observed_query_names = sorted({p.split("=", 1)[0] for p in parts.query.split("&") if p})
        is_spring_page = isinstance(payload, dict) and isinstance(payload.get("pageable"), dict)
        query = f"size={SPRING_PAGE_SIZE}" if is_spring_page else ""
        endpoint = urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))

        # Without a link field, identity comes from the record id (like ICS's
        # UID), so a URL is not required of every event.
        has_url = "canonical_url" in paths
        required = list(DEFAULT_REQUIRED_FIELDS)
        if not has_url and "external_source_id" in paths:
            required = [f for f in required if f != "canonical_url"]

        field_candidates: list[FieldSelectorCandidate] = []
        missing: list[str] = []
        min_cov = getattr(context.policy, "min_field_coverage", 0.5)
        for engine_field, source_key in _REQUIRED_SOURCE.items():
            if engine_field not in required:
                continue
            path = paths.get(source_key)
            coverage, samples = _coverage(records, path)
            accepted = coverage >= min_cov
            field_candidates.append(
                FieldSelectorCandidate(
                    field=engine_field, kind="json_path", selector=path, attribute=None,
                    confidence=round(0.6 + 0.35 * coverage, 4), coverage=coverage,
                    parse_success_rate=None,
                    evidence=(f"{root}[].{path}",) if path else (),
                    sample_values=tuple(samples), warnings=(), alternatives=(),
                    accepted=accepted,
                )
            )
            if not accepted:
                missing.append(engine_field)

        try:
            configuration = SiteConfiguration(
                pattern_name=NAME,
                api_endpoint=endpoint,
                timezone=context.fallback_timezone,
                json_paths={"events_root": root, **paths},
                pagination={
                    "strategy": "page_metadata",
                    "page_param": "page",
                    "max_pages": context.policy.max_pages,
                    "max_events": context.policy.max_events,
                },
                max_detail_fetches=0,
                required_fields=required,
            )
        except ValueError as exc:
            return failed_proposal(f"proposed paged JSON configuration failed validation: {exc}")

        notes = [
            f"paged JSON events API at {endpoint}",
            f"record array: {root}",
            "field paths: " + ", ".join(f"{k}={v}" for k, v in sorted(paths.items())),
            "pagination follows the response's own page metadata",
        ]
        if not has_url and "canonical_url" not in required:
            notes.append(
                "records carry no event link; events are identified by their id and the "
                "link is not required"
            )
        warnings: list[str] = []
        if observed_query_names:
            warnings.append(
                f"observed query parameters {observed_query_names} were not persisted "
                "(they may carry tokens or a page position); configure required filters "
                "manually."
            )
        return ConfigurationProposal(
            configuration=configuration,
            field_candidates=tuple(field_candidates),
            confidence=round(min(0.9, 0.5 + 0.4 * context.detection.confidence), 4),
            missing_required_fields=tuple(missing),
            warnings=tuple(warnings),
            notes=tuple(notes),
        )
