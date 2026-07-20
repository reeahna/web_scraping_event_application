"""Optional detail-page enrichment for the generic_html_cards pattern.

Kept separate from patterns/static_html.py so the pattern itself stays a
pure function of one response. A detail-page fetch goes through the exact
same FetchStrategy as the listing page — same SSRF/redirect protections,
same byte cap, same retry/backoff — because it's the identical call, not a
separately-trusted code path.

A `field_selectors` entry named `detail_<field>` (e.g. `detail_description`)
is resolved against the detail page and merges into the *base* field name
(`description`) on the candidate's `raw` dict — enriching/overwriting the
listing page's shorter value with the detail page's fuller one, not adding
a separate `detail_description` field.
"""

from __future__ import annotations

import dataclasses

from bs4 import BeautifulSoup

from app.extraction.fetch import FetchStrategy
from app.extraction.selectors import resolve_css
from app.extraction.types import EventCandidate, FetchRequest
from app.schemas.extraction import SiteConfiguration

_DETAIL_FIELD_PREFIX = "detail_"


async def enrich_with_detail_pages(
    candidates: list[EventCandidate],
    fetch: FetchStrategy,
    config: SiteConfiguration,
) -> list[EventCandidate]:
    detail_selectors = {
        name[len(_DETAIL_FIELD_PREFIX) :]: selector
        for name, selector in config.field_selectors.items()
        if name.startswith(_DETAIL_FIELD_PREFIX)
    }
    if not detail_selectors:
        return candidates

    enriched: list[EventCandidate] = []
    fetches_used = 0
    for candidate in candidates:
        detail_link = candidate.raw.get("detail_link")
        if not detail_link or fetches_used >= config.max_detail_fetches:
            enriched.append(candidate)
            continue

        fetches_used += 1
        response = await fetch.fetch(FetchRequest(url=str(detail_link)), config.fetch)
        if response.blocked_reason is not None or response.status_code != 200:
            enriched.append(
                dataclasses.replace(
                    candidate,
                    warnings=(*candidate.warnings, f"detail_page_fetch_failed:{detail_link}"),
                )
            )
            continue

        soup = BeautifulSoup(response.text, "html.parser")
        new_raw = dict(candidate.raw)
        new_source_paths = dict(candidate.field_source_paths)
        new_warnings = list(candidate.warnings)
        for field_name, selector_config in detail_selectors.items():
            result = resolve_css(soup, selector_config.selector, selector_config.attribute)
            if result.value is not None:
                new_raw[field_name] = result.value
                if result.source_path:
                    new_source_paths[field_name] = f"detail:{result.source_path}"
            new_warnings.extend(f"detail_{field_name}: {w}" for w in result.warnings)

        enriched.append(
            dataclasses.replace(
                candidate,
                raw=new_raw,
                field_source_paths=new_source_paths,
                warnings=tuple(new_warnings),
            )
        )
    return enriched
