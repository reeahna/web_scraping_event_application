"""Configuration proposers for the JSON-in-script patterns.

These have no fixed response schema — the events live at a site-specific path
inside an arbitrary JSON document — so the proposer discovers that path with
`json_events.find_event_arrays` (deterministic scoring, never a blind guess)
and maps fields from a real sample object with `json_events.infer_field_paths`.
When no array scores as an event list, it proposes nothing and reports why, so
the source lands in review rather than being mis-configured.
"""

from __future__ import annotations

import json
from typing import Any

from bs4 import BeautifulSoup

from app.extraction.inference.base import DEFAULT_REQUIRED_FIELDS, failed_proposal
from app.extraction.inference.json_events import find_event_arrays, infer_field_paths
from app.extraction.inference.types import (
    ConfigurationProposal,
    FieldSelectorCandidate,
    ProposalContext,
)
from app.extraction.selectors import resolve_json_path
from app.schemas.extraction import SiteConfiguration

_REQUIRED_JSON_FIELDS = ("title", "canonical_url", "start_datetime")


def _first_sample(document: Any, events_root: str) -> dict | None:
    resolved = resolve_json_path(document, events_root).value if events_root != "$" else document
    if isinstance(resolved, list):
        for item in resolved:
            if isinstance(item, dict):
                return item
    return None


class _JsonEventsProposer:
    """Shared body; subclasses only differ in how they obtain the document
    and their pattern name / listing-vs-endpoint choice."""

    pattern_name = ""

    def _document(self, context: ProposalContext) -> Any | None:  # pragma: no cover - overridden
        raise NotImplementedError

    def propose(self, context: ProposalContext) -> ConfigurationProposal:
        document = self._document(context)
        if document is None:
            return failed_proposal(f"no parseable JSON document was found for {self.pattern_name}")

        candidates = find_event_arrays(document)
        if not candidates:
            return failed_proposal(
                "no JSON array in the page scored as an event list", warnings=("no_event_array",)
            )
        best = candidates[0]
        sample = _first_sample(document, best.path)
        if sample is None:
            return failed_proposal("the discovered event array had no usable object")

        field_paths = infer_field_paths(sample)
        json_paths = {"events_root": best.path, **field_paths}

        missing = [
            "start_date" if f == "start_datetime" else f
            for f in _REQUIRED_JSON_FIELDS
            if f not in field_paths
        ]

        try:
            configuration = SiteConfiguration(
                pattern_name=self.pattern_name,
                listing_url=context.listing_url,
                timezone=context.fallback_timezone,
                json_paths=json_paths,
                pagination={"strategy": "none", "max_pages": context.policy.max_pages,
                            "max_events": context.policy.max_events},
                max_detail_fetches=0,
                required_fields=list(DEFAULT_REQUIRED_FIELDS),
            )
        except ValueError as exc:
            return failed_proposal(f"proposed configuration failed validation: {exc}")

        candidates_out = tuple(
            FieldSelectorCandidate(
                field=field,
                kind="json_path",
                selector=path,
                attribute=None,
                confidence=round(best.event_like_rate, 4),
                coverage=best.event_like_rate,
                parse_success_rate=None,
                evidence=(f"discovered at {best.path} ({best.size} items, "
                          f"{best.event_like_rate:.0%} event-like)",),
                sample_values=(str(resolve_json_path(sample, path).value)[:120],),
                warnings=(),
                alternatives=(),
                accepted=True,
            )
            for field, path in field_paths.items()
        )
        return ConfigurationProposal(
            configuration=configuration,
            field_candidates=candidates_out,
            confidence=round(min(0.9, 0.5 + 0.4 * best.event_like_rate), 4),
            missing_required_fields=tuple(missing),
            notes=(
                f"event array discovered at '{best.path}' with {best.size} items",
                f"other candidate arrays considered: {len(candidates) - 1}",
                "field mappings stay editable under 'JSON paths' in advanced configuration",
            ),
        )


class EmbeddedJsonProposer(_JsonEventsProposer):
    pattern_name = "embedded_json"

    def _document(self, context: ProposalContext) -> Any | None:
        soup = BeautifulSoup(context.response.text, "html.parser")
        best_doc, best_len = None, -1
        for script in soup.find_all("script", attrs={"type": "application/json"}):
            text = script.string or script.get_text()
            if not text or not text.strip():
                continue
            try:
                doc = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                continue
            if len(text) > best_len:
                best_doc, best_len = doc, len(text)
        return best_doc


class NextDataProposer(_JsonEventsProposer):
    pattern_name = "next_data"

    def _document(self, context: ProposalContext) -> Any | None:
        from app.extraction.patterns.next_data import parse_next_data

        return parse_next_data(context.response.text)


class NuxtPayloadProposer(_JsonEventsProposer):
    pattern_name = "nuxt_payload"

    def _document(self, context: ProposalContext) -> Any | None:
        from app.extraction.patterns.nuxt_payload import parse_nuxt_payload

        return parse_nuxt_payload(context.response.text)
