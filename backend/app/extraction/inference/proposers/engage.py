"""Configuration proposer for Campus Labs Engage (`campus_labs_engage`).

The discovery API only returns upcoming events when asked for them
(`endsAfter`), and pages with `skip`/`take`, so the proposal is a request
recipe: `endsAfter` is the dynamic start of today's window and `skip`/`take`
are the page cursor, all resolved fresh on every run over plain HTTP. Nothing
here names a school; the endpoint comes from the detection result.
"""

from __future__ import annotations

from app.extraction.inference.base import DEFAULT_REQUIRED_FIELDS, failed_proposal
from app.extraction.inference.types import ConfigurationProposal, ProposalContext
from app.extraction.patterns.campus_labs_engage import NAME, engage_base
from app.schemas.extraction import SiteConfiguration
from app.schemas.request_recipe import (
    RecipePagination,
    RecipeValue,
    RecipeWindow,
    RequestRecipe,
)

PAGE_SIZE = 50


def _literal(value: str) -> RecipeValue:
    return RecipeValue(kind="literal", value=value)


def _placeholder(kind: str) -> RecipeValue:
    return RecipeValue(kind="json_template", value={"kind": kind})


class CampusLabsEngageProposer:
    pattern_name = NAME

    def propose(self, context: ProposalContext) -> ConfigurationProposal:
        endpoint = next((e for e in context.detection.discovered_endpoints if e), None)
        if not endpoint:
            return failed_proposal("no Engage discovery endpoint could be derived")
        max_pages = max(1, min(100, context.policy.max_pages))
        try:
            recipe = RequestRecipe(
                method="GET",
                endpoint=endpoint,
                query_params={
                    "endsAfter": _placeholder("window_start_utc"),
                    "orderByField": _literal("endsOn"),
                    "orderByDirection": _literal("ascending"),
                    "status": _literal("Approved"),
                    "take": _placeholder("page_limit"),
                    "skip": _placeholder("page_offset"),
                    "query": _literal(""),
                },
                headers={"Accept": _literal("application/json")},
                source_page_url=engage_base(endpoint) + "/events",
                window=RecipeWindow(horizon_days=180, start_of_day=True),
                pagination=RecipePagination(kind="offset", limit=PAGE_SIZE, max_pages=max_pages),
            )
            configuration = SiteConfiguration(
                pattern_name=NAME,
                api_endpoint=endpoint,
                timezone=context.fallback_timezone,
                json_paths={"events_root": "value"},
                pagination={
                    "strategy": "none",
                    "max_pages": context.policy.max_pages,
                    "max_events": context.policy.max_events,
                },
                max_detail_fetches=0,
                required_fields=list(DEFAULT_REQUIRED_FIELDS),
                request_recipe=recipe,
            )
        except ValueError as exc:
            return failed_proposal(f"proposed Engage configuration failed validation: {exc}")
        return ConfigurationProposal(
            configuration=configuration,
            confidence=round(min(0.9, 0.5 + 0.4 * context.detection.confidence), 4),
            notes=(
                f"Campus Labs Engage discovery API at {endpoint}",
                "upcoming events only (endsAfter = start of today), "
                f"{PAGE_SIZE} per page via skip/take",
                "event links follow the platform's /event/{id} route",
            ),
        )
