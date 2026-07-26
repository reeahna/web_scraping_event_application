"""Configuration proposers for the feed and search-API patterns.

ICS proposes a working config directly (the feed's shape is fixed). RSS/Atom
can map identity and links but not the event date — a feed entry's publication
date is not its event date — so unless a date-bearing element is present it
reports `start_date` missing and lands in review. Algolia never proposes an
approvable config here: querying needs a search-only key, and until that key
handling is proven safe the source stays in review and no query is attempted.
"""

from __future__ import annotations

from app.extraction.inference.base import failed_proposal
from app.extraction.inference.types import ConfigurationProposal, ProposalContext
from app.schemas.extraction import SiteConfiguration

# ICS events routinely have no public URL; the pattern never fabricates one, so
# canonical_url is not required and dedup falls back to the UID.
_ICS_REQUIRED = ["title", "start_date"]
_FEED_CONTENT_TYPES_ICS = ("text/calendar", "text/plain")
_FEED_CONTENT_TYPES_XML = (
    "application/rss+xml",
    "application/atom+xml",
    "application/xml",
    "text/xml",
)

# Element local-names that plausibly carry a real event start date (never the
# publication date). Only these are proposed as the date source.
_EVENT_DATE_ELEMENTS = ("start", "startdate", "start_date", "dtstart", "eventdate", "begin", "when")


class IcsCalendarProposer:
    pattern_name = "ics_calendar"

    def propose(self, context: ProposalContext) -> ConfigurationProposal:
        text = context.response.text
        if "begin:vcalendar" not in text[:2000].lower() or "begin:vevent" not in text.lower():
            return failed_proposal("the response is not an iCalendar feed with events")
        try:
            configuration = SiteConfiguration(
                pattern_name=self.pattern_name,
                api_endpoint=context.listing_url,
                timezone=context.fallback_timezone,
                fetch={"allowed_content_types": _FEED_CONTENT_TYPES_ICS},
                pagination={"strategy": "none", "max_pages": context.policy.max_pages,
                            "max_events": context.policy.max_events},
                max_detail_fetches=0,
                # No canonical_url requirement: ICS identity is the UID.
                required_fields=_ICS_REQUIRED,
            )
        except ValueError as exc:
            return failed_proposal(f"proposed configuration failed validation: {exc}")
        return ConfigurationProposal(
            configuration=configuration,
            confidence=0.85,
            notes=(
                "iCalendar feed detected; fields map from VEVENT (SUMMARY, DTSTART, UID, ...)",
                "canonical_url is not required — ICS events identify by UID",
                "recurrence rules are preserved for the Phase 8G expander",
            ),
        )


class RssAtomProposer:
    pattern_name = "rss_atom_events"

    def propose(self, context: ProposalContext) -> ConfigurationProposal:
        from xml.etree.ElementTree import Element

        from defusedxml.ElementTree import ParseError, fromstring

        try:
            root = fromstring(context.response.text)
        except (ParseError, ValueError):
            return failed_proposal("the response is not parseable feed XML")

        def local(tag: str) -> str:
            return tag.rsplit("}", 1)[-1] if "}" in tag else tag

        first_item: Element | None = next(
            (e for e in root.iter() if local(e.tag) in ("item", "entry")), None
        )
        if first_item is None:
            return failed_proposal("the feed has no items or entries")

        child_names = {local(c.tag) for c in first_item}
        date_element = next((n for n in _EVENT_DATE_ELEMENTS if n in child_names), None)

        json_paths: dict[str, str] = {}
        if date_element:
            json_paths["start_datetime"] = date_element

        try:
            configuration = SiteConfiguration(
                pattern_name=self.pattern_name,
                listing_url=context.listing_url,
                timezone=context.fallback_timezone,
                fetch={"allowed_content_types": _FEED_CONTENT_TYPES_XML},
                json_paths=json_paths,
                pagination={"strategy": "none", "max_pages": context.policy.max_pages,
                            "max_events": context.policy.max_events},
                max_detail_fetches=0,
                required_fields=["title", "start_date"],
            )
        except ValueError as exc:
            return failed_proposal(f"proposed configuration failed validation: {exc}")

        missing = () if date_element else ("start_date",)
        notes = ["feed detected; title/link/id mapped from standard elements"]
        if date_element:
            notes.append(f"event date mapped from <{date_element}> (not the publication date)")
        else:
            notes.append(
                "no event-date element found — a generic feed's publication date is not used "
                "as the event date, so this needs a configured date element and manual review"
            )
        return ConfigurationProposal(
            configuration=configuration,
            confidence=0.7 if date_element else 0.4,
            missing_required_fields=missing,
            warnings=() if date_element else ("no_event_date_element",),
            notes=tuple(notes),
        )


class AlgoliaSearchProposer:
    pattern_name = "algolia_search"

    def propose(self, context: ProposalContext) -> ConfigurationProposal:
        # Querying Algolia needs a search-only key. Safe key handling cannot be
        # established from a page alone, so — per policy — the source is left in
        # review and no query is proposed or attempted.
        return failed_proposal(
            "Algolia querying requires a search-only key supplied as a secret reference "
            "(fetch.secret_header_refs); configure the app id, index and key reference, then "
            "re-detect. No query is made until a key reference is present.",
            warnings=("algolia_requires_key_reference",),
        )
