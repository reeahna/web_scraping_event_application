"""PatternConfigurationProposer: the one extension point patterns implement
to make themselves automatically configurable.

A proposer is a pure function of a `ProposalContext` — one already-fetched
response, its detection result, the listing URL, a fallback timezone, and the
policy. It performs no I/O and receives no Website row, so a proposer cannot
branch on a hostname or a site name even by accident. Dispatch is a registry
lookup (`PatternRegistration.proposer`), exactly like detector/extractor
dispatch.
"""

from __future__ import annotations

from typing import Protocol

from app.extraction.inference.types import ConfigurationProposal, ProposalContext

# The engine's minimal required set — deliberately not widened by inference.
# image/venue/address/description are proposed whenever they are confidently
# observable, but requiring one would reject otherwise-good events that
# simply don't carry it.
DEFAULT_REQUIRED_FIELDS: tuple[str, ...] = ("title", "start_date", "canonical_url")


class PatternConfigurationProposer(Protocol):
    pattern_name: str

    def propose(self, context: ProposalContext) -> ConfigurationProposal: ...


def failed_proposal(error: str, *, warnings: tuple[str, ...] = ()) -> ConfigurationProposal:
    return ConfigurationProposal(configuration=None, error=error, warnings=warnings)
