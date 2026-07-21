"""PatternRegistry: the single dispatch point mapping a stable pattern name
to its detector + extractor + config schema + metadata. A dict lookup is the
ONLY dispatch mechanism in this engine — there is no per-site/domain
conditional anywhere in app.extraction or app.services.extraction_runs.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.extraction.detection import (
    RELIABILITY_ORDER,
    JsonLdDetector,
    LiveWhaleDetector,
    PatternDetector,
    StaticHtmlDetector,
    TheEventsCalendarDetector,
    WordPressRestDetector,
)
from app.extraction.inference.base import PatternConfigurationProposer
from app.extraction.inference.proposers.generic_html import GenericHtmlCardsProposer
from app.extraction.inference.proposers.structured import (
    JsonLdEventProposer,
    livewhale_proposer,
    the_events_calendar_proposer,
    wordpress_rest_proposer,
)
from app.extraction.patterns.base import ExtractionPattern
from app.extraction.patterns.jsonld import PATTERN_VERSION as _JSONLD_VERSION
from app.extraction.patterns.jsonld import JsonLdEventPattern
from app.extraction.patterns.livewhale_json import PATTERN_VERSION as _LW_VERSION
from app.extraction.patterns.livewhale_json import LiveWhalePattern
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
            supported_pagination=("none", "query_param", "next_link"),
            proposer=JsonLdEventProposer(),
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
            supported_pagination=("none", "query_param", "next_link"),
            proposer=GenericHtmlCardsProposer(),
        )
    )
    return registry


REGISTRY = build_default_registry()
