"""Generic static HTML event-card extraction pattern.

Every field is resolved via a CSS selector from `config.field_selectors` —
there is no per-site Python subclass, only data. Detail-page enrichment (a
second, optional fetch per card) is handled by the orchestration layer
(app.extraction.detail_pages), not here — this pattern only resolves the
detail link and hands it back as a raw field, so the second fetch reuses
the exact same SSRF-protected fetch path as the listing page.
"""

from __future__ import annotations

import hashlib
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from app.extraction.selectors import resolve_css
from app.extraction.types import EventCandidate, FetchResponse
from app.schemas.extraction import SiteConfiguration

NAME = "generic_html_cards"
PATTERN_VERSION = "1"


def _resolve_field(
    card: Tag, config: SiteConfiguration, field_name: str
) -> tuple[object, str | None, tuple[str, ...]]:
    selector_config = config.field_selectors.get(field_name)
    if selector_config is None:
        return None, None, ()
    result = resolve_css(card, selector_config.selector, selector_config.attribute)
    return result.value, result.source_path, result.warnings


class StaticHtmlCardsPattern:
    name = NAME

    def extract(self, response: FetchResponse, config: SiteConfiguration) -> list[EventCandidate]:
        if not config.event_container_selector:
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        cards = soup.select(config.event_container_selector)

        candidates: list[EventCandidate] = []
        for card in cards:
            raw: dict[str, object] = {}
            field_source_paths: dict[str, str] = {}
            warnings: list[str] = []

            for field_name in (
                "title",
                "canonical_url",
                "description",
                "start_datetime",
                "start_time",
                "end_datetime",
                "end_time",
                "venue",
                "address",
                "image",
                "source_category",
                "external_source_id",
                "detail_link",
            ):
                value, source_path, field_warnings = _resolve_field(card, config, field_name)
                raw[field_name] = value
                if source_path:
                    field_source_paths[field_name] = source_path
                warnings.extend(f"{field_name}: {w}" for w in field_warnings)

            base_url = response.final_url
            if raw.get("canonical_url"):
                raw["canonical_url"] = urljoin(base_url, str(raw["canonical_url"]))
            if raw.get("image"):
                raw["image"] = urljoin(base_url, str(raw["image"]))
            if raw.get("detail_link"):
                raw["detail_link"] = urljoin(base_url, str(raw["detail_link"]))

            if not raw.get("canonical_url") and config.allow_page_url_as_canonical_fallback:
                raw["canonical_url"] = base_url
                field_source_paths["canonical_url"] = "fallback:page_url"

            # Hash the card's own serialized markup (not the whole page) so
            # the record hash is specific to this candidate.
            raw_record_hash = hashlib.sha256(str(card).encode("utf-8")).hexdigest()

            candidates.append(
                EventCandidate(
                    raw=raw,
                    title=None,
                    canonical_url=None,
                    description=None,
                    start_date=None,
                    start_time=None,
                    end_date=None,
                    end_time=None,
                    timezone=None,
                    venue=None,
                    address=None,
                    image_url=None,
                    latitude=None,
                    longitude=None,
                    source_category=None,
                    external_source_id=None,
                    field_source_paths=field_source_paths,
                    transformation_history=(),
                    source_page=response.final_url,
                    extraction_pattern=NAME,
                    warnings=tuple(warnings),
                    raw_record_hash=raw_record_hash,
                )
            )
        return candidates
