"""Configuration proposer for BiblioCommons library events
(`bibliocommons_events`).

The gateway API answers 100 events per page and is walked with `path_page`
pagination over a URL template ending in `page={page}`. The endpoint comes from
the detection result (the platform's route for the library), so nothing here
names a library.
"""

from __future__ import annotations

from app.extraction.inference.base import DEFAULT_REQUIRED_FIELDS, failed_proposal
from app.extraction.inference.types import ConfigurationProposal, ProposalContext
from app.extraction.patterns.bibliocommons_events import NAME
from app.schemas.extraction import SiteConfiguration


class BiblioCommonsEventsProposer:
    pattern_name = NAME

    def propose(self, context: ProposalContext) -> ConfigurationProposal:
        endpoint = next((e for e in context.detection.discovered_endpoints if e), None)
        if not endpoint:
            return failed_proposal("no BiblioCommons gateway endpoint could be derived")
        try:
            configuration = SiteConfiguration(
                pattern_name=NAME,
                api_endpoint=f"{endpoint}&page=1",
                timezone=context.fallback_timezone,
                pagination={
                    "strategy": "path_page",
                    "page_path_template": f"{endpoint}&page={{page}}",
                    "max_pages": context.policy.max_pages,
                    "max_events": context.policy.max_events,
                },
                max_detail_fetches=0,
                required_fields=list(DEFAULT_REQUIRED_FIELDS),
            )
        except ValueError as exc:
            return failed_proposal(f"proposed BiblioCommons configuration failed validation: {exc}")
        return ConfigurationProposal(
            configuration=configuration,
            confidence=round(min(0.9, 0.5 + 0.4 * context.detection.confidence), 4),
            notes=(
                f"BiblioCommons events gateway at {endpoint}",
                "100 events per page, walked with page={page}",
                "event links follow the platform's /events/{id} route",
            ),
        )
