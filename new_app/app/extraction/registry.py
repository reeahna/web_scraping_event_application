"""PatternRegistry: the single dispatch point mapping a stable pattern name
to its detector + extractor + config schema + metadata. A dict lookup is the
ONLY dispatch mechanism in this engine — there is no per-site/domain
conditional anywhere in app.extraction or app.services.extraction_runs.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.extraction.detection import (
    RELIABILITY_ORDER,
    AlgoliaSearchDetector,
    EmbeddedJsonDetector,
    IcsCalendarDetector,
    InlineJsonEventsDetector,
    JsonLdDetector,
    LiveWhaleDetector,
    NextDataDetector,
    NuxtPayloadDetector,
    PatternDetector,
    RssAtomDetector,
    SimpleviewEventsDetector,
    StaticHtmlDetector,
    TheEventsCalendarDetector,
    WordPressRestDetector,
)
from app.extraction.inference.base import PatternConfigurationProposer
from app.extraction.inference.proposers.feeds import (
    AlgoliaSearchProposer,
    IcsCalendarProposer,
    RssAtomProposer,
)
from app.extraction.inference.proposers.generic_html import GenericHtmlCardsProposer
from app.extraction.inference.proposers.json_scripts import (
    EmbeddedJsonProposer,
    NextDataProposer,
    NuxtPayloadProposer,
)
from app.extraction.inference.proposers.simpleview import simpleview_events_proposer
from app.extraction.inference.proposers.structured import (
    InlineJsonEventsProposer,
    JsonLdEventProposer,
    livewhale_proposer,
    the_events_calendar_proposer,
    wordpress_rest_proposer,
)
from app.extraction.patterns.algolia_search import PATTERN_VERSION as _ALGOLIA_VERSION
from app.extraction.patterns.algolia_search import AlgoliaSearchPattern
from app.extraction.patterns.base import ExtractionPattern
from app.extraction.patterns.embedded_json import PATTERN_VERSION as _EMBEDDED_VERSION
from app.extraction.patterns.embedded_json import EmbeddedJsonPattern
from app.extraction.patterns.ics_calendar import PATTERN_VERSION as _ICS_VERSION
from app.extraction.patterns.ics_calendar import IcsCalendarPattern
from app.extraction.patterns.inline_json import PATTERN_VERSION as _INLINE_JSON_VERSION
from app.extraction.patterns.inline_json import InlineJsonEventsPattern
from app.extraction.patterns.jsonld import PATTERN_VERSION as _JSONLD_VERSION
from app.extraction.patterns.jsonld import JsonLdEventPattern
from app.extraction.patterns.livewhale_json import PATTERN_VERSION as _LW_VERSION
from app.extraction.patterns.livewhale_json import LiveWhalePattern
from app.extraction.patterns.next_data import PATTERN_VERSION as _NEXT_VERSION
from app.extraction.patterns.next_data import NextDataPattern
from app.extraction.patterns.nuxt_payload import PATTERN_VERSION as _NUXT_VERSION
from app.extraction.patterns.nuxt_payload import NuxtPayloadPattern
from app.extraction.patterns.rss_atom_events import PATTERN_VERSION as _RSS_VERSION
from app.extraction.patterns.rss_atom_events import RssAtomEventsPattern
from app.extraction.patterns.simpleview_events import PATTERN_VERSION as _SIMPLEVIEW_VERSION
from app.extraction.patterns.simpleview_events import SimpleviewEventsPattern
from app.extraction.patterns.static_html import PATTERN_VERSION as _HTML_VERSION
from app.extraction.patterns.static_html import StaticHtmlCardsPattern
from app.extraction.patterns.the_events_calendar import PATTERN_VERSION as _TEC_VERSION
from app.extraction.patterns.the_events_calendar import TheEventsCalendarPattern
from app.extraction.patterns.wordpress_rest import PATTERN_VERSION as _WP_VERSION
from app.extraction.patterns.wordpress_rest import WordPressRestPattern
from app.schemas.extraction import SiteConfiguration


class DuplicatePatternError(ValueError):
    pass


class UnsupportedPatternError(ValueError):
    pass


@dataclass(frozen=True)
class PatternRegistration:
    name: str
    detector: PatternDetector
    extractor: ExtractionPattern
    config_schema: type[SiteConfiguration]
    priority: int
    version: str
    browser_required: bool
    supported_pagination: tuple[str, ...]
    # Optional so a pattern can be registered before it is automatically
    # configurable; a pattern without one simply falls back to the manual
    # configuration form instead of participating in automatic onboarding.
    proposer: PatternConfigurationProposer | None = None
    # Presentation-only metadata, kept here (the single source of truth for a
    # pattern) rather than hardcoded in a router or template. `classification`
    # is a coarse family: "structured" (an API/feed/JSON source), "static"
    # (scraped HTML markup), or "browser" (needs a rendered page).
    display_name: str = ""
    classification: str = "structured"

    @property
    def label(self) -> str:
        return self.display_name or self.name

    @property
    def has_proposer(self) -> bool:
        return self.proposer is not None


def pattern_options(registry: PatternRegistry, evidence: dict | None = None) -> list[dict]:
    """Registry-driven metadata for the manual pattern selector, in reliability
    order. `evidence` is the per-detector `all_results` map from a detection
    run (or None); a pattern is marked as having supporting evidence when its
    detector produced a non-zero confidence on the current response.

    Never hardcodes a pattern name — the roster and every field come from the
    registry, so registering a pattern makes it appear automatically.
    """
    evidence = evidence or {}
    options = []
    for name in registry.names():
        registration = registry.get(name)
        detector_result = evidence.get(name)
        confidence = (
            detector_result.get("confidence") if isinstance(detector_result, dict) else None
        )
        options.append(
            {
                "name": name,
                "display_name": registration.label,
                "classification": registration.classification,
                "has_proposer": registration.has_proposer,
                "browser_required": registration.browser_required,
                "evidence_confidence": confidence,
                "has_evidence": bool(confidence),
            }
        )
    options.sort(key=lambda option: registry.get(option["name"]).priority)
    return options


class PatternRegistry:
    def __init__(self) -> None:
        self._patterns: dict[str, PatternRegistration] = {}

    def register(self, registration: PatternRegistration) -> None:
        if registration.name in self._patterns:
            raise DuplicatePatternError(f"Pattern '{registration.name}' is already registered")
        self._patterns[registration.name] = registration

    def get(self, name: str) -> PatternRegistration:
        try:
            return self._patterns[name]
        except KeyError:
            raise UnsupportedPatternError(f"Unknown extraction pattern: {name}") from None

    def names(self) -> tuple[str, ...]:
        return tuple(self._patterns.keys())

    def __contains__(self, name: str) -> bool:
        return name in self._patterns


def build_default_registry() -> PatternRegistry:
    registry = PatternRegistry()
    registry.register(
        PatternRegistration(
            name="wordpress_rest",
            detector=WordPressRestDetector(),
            extractor=WordPressRestPattern(),
            config_schema=SiteConfiguration,
            priority=RELIABILITY_ORDER.index("wordpress_rest"),
            version=_WP_VERSION,
            browser_required=False,
            supported_pagination=("none", "wordpress"),
            proposer=wordpress_rest_proposer(),
            display_name="WordPress REST API",
            classification="structured",
        )
    )
    registry.register(
        PatternRegistration(
            name="the_events_calendar",
            detector=TheEventsCalendarDetector(),
            extractor=TheEventsCalendarPattern(),
            config_schema=SiteConfiguration,
            priority=RELIABILITY_ORDER.index("the_events_calendar"),
            version=_TEC_VERSION,
            browser_required=False,
            supported_pagination=("none", "tribe_rest"),
            proposer=the_events_calendar_proposer(),
            display_name="The Events Calendar (Tribe)",
            classification="structured",
        )
    )
    registry.register(
        PatternRegistration(
            name="livewhale_json",
            detector=LiveWhaleDetector(),
            extractor=LiveWhalePattern(),
            config_schema=SiteConfiguration,
            priority=RELIABILITY_ORDER.index("livewhale_json"),
            version=_LW_VERSION,
            browser_required=False,
            supported_pagination=("none", "livewhale_offset"),
            proposer=livewhale_proposer(),
            display_name="LiveWhale JSON feed",
            classification="structured",
        )
    )
    registry.register(
        PatternRegistration(
            name="simpleview_events",
            detector=SimpleviewEventsDetector(),
            extractor=SimpleviewEventsPattern(),
            config_schema=SiteConfiguration,
            priority=RELIABILITY_ORDER.index("simpleview_events"),
            version=_SIMPLEVIEW_VERSION,
            browser_required=False,
            # Pagination is not yet confirmed for the find endpoint; single
            # response by default, with query_param available once confirmed.
            supported_pagination=("none", "query_param"),
            proposer=simpleview_events_proposer(),
            display_name="Simpleview Events API",
            classification="structured",
        )
    )
    registry.register(
        PatternRegistration(
            name="json_ld_event",
            detector=JsonLdDetector(),
            extractor=JsonLdEventPattern(),
            config_schema=SiteConfiguration,
            priority=RELIABILITY_ORDER.index("json_ld_event"),
            version=_JSONLD_VERSION,
            browser_required=False,
            supported_pagination=("none", "query_param", "next_link", "path_page"),
            proposer=JsonLdEventProposer(),
            display_name="schema.org JSON-LD Event",
            classification="structured",
        )
    )
    registry.register(
        PatternRegistration(
            name="next_data",
            detector=NextDataDetector(),
            extractor=NextDataPattern(),
            config_schema=SiteConfiguration,
            priority=RELIABILITY_ORDER.index("next_data"),
            version=_NEXT_VERSION,
            browser_required=False,
            supported_pagination=("none", "query_param"),
            proposer=NextDataProposer(),
            display_name="Next.js __NEXT_DATA__",
            classification="structured",
        )
    )
    registry.register(
        PatternRegistration(
            name="nuxt_payload",
            detector=NuxtPayloadDetector(),
            extractor=NuxtPayloadPattern(),
            config_schema=SiteConfiguration,
            priority=RELIABILITY_ORDER.index("nuxt_payload"),
            version=_NUXT_VERSION,
            browser_required=False,
            supported_pagination=("none", "query_param"),
            proposer=NuxtPayloadProposer(),
            display_name="Nuxt __NUXT_DATA__",
            classification="structured",
        )
    )
    registry.register(
        PatternRegistration(
            name="embedded_json",
            detector=EmbeddedJsonDetector(),
            extractor=EmbeddedJsonPattern(),
            config_schema=SiteConfiguration,
            priority=RELIABILITY_ORDER.index("embedded_json"),
            version=_EMBEDDED_VERSION,
            browser_required=False,
            supported_pagination=("none", "query_param"),
            proposer=EmbeddedJsonProposer(),
            display_name="Embedded JSON (<script>)",
            classification="structured",
        )
    )
    registry.register(
        PatternRegistration(
            name="inline_json_events",
            detector=InlineJsonEventsDetector(),
            extractor=InlineJsonEventsPattern(),
            config_schema=SiteConfiguration,
            priority=RELIABILITY_ORDER.index("inline_json_events"),
            version=_INLINE_JSON_VERSION,
            browser_required=False,
            supported_pagination=("none",),
            proposer=InlineJsonEventsProposer(),
            display_name="Inline JSON variable (window.x = [...])",
            classification="structured",
        )
    )
    registry.register(
        PatternRegistration(
            name="ics_calendar",
            detector=IcsCalendarDetector(),
            extractor=IcsCalendarPattern(),
            config_schema=SiteConfiguration,
            priority=RELIABILITY_ORDER.index("ics_calendar"),
            version=_ICS_VERSION,
            browser_required=False,
            supported_pagination=("none",),
            proposer=IcsCalendarProposer(),
            display_name="iCalendar / ICS feed",
            classification="structured",
        )
    )
    registry.register(
        PatternRegistration(
            name="rss_atom_events",
            detector=RssAtomDetector(),
            extractor=RssAtomEventsPattern(),
            config_schema=SiteConfiguration,
            priority=RELIABILITY_ORDER.index("rss_atom_events"),
            version=_RSS_VERSION,
            browser_required=False,
            supported_pagination=("none", "query_param"),
            proposer=RssAtomProposer(),
            display_name="RSS / Atom feed",
            classification="structured",
        )
    )
    registry.register(
        PatternRegistration(
            name="algolia_search",
            detector=AlgoliaSearchDetector(),
            extractor=AlgoliaSearchPattern(),
            config_schema=SiteConfiguration,
            priority=RELIABILITY_ORDER.index("algolia_search"),
            version=_ALGOLIA_VERSION,
            browser_required=False,
            supported_pagination=("none", "query_param"),
            proposer=AlgoliaSearchProposer(),
            display_name="Algolia search API",
            classification="structured",
        )
    )
    registry.register(
        PatternRegistration(
            name="generic_html_cards",
            detector=StaticHtmlDetector(),
            extractor=StaticHtmlCardsPattern(),
            config_schema=SiteConfiguration,
            priority=RELIABILITY_ORDER.index("generic_html_cards"),
            version=_HTML_VERSION,
            browser_required=False,
            supported_pagination=("none", "query_param", "next_link", "path_page"),
            proposer=GenericHtmlCardsProposer(),
            display_name="Generic HTML cards",
            classification="static",
        )
    )
    return registry


REGISTRY = build_default_registry()
