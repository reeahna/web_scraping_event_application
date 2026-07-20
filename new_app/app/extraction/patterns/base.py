from typing import Protocol

from app.extraction.types import EventCandidate, FetchResponse
from app.schemas.extraction import SiteConfiguration


class ExtractionPattern(Protocol):
    """Produces *raw* candidates only — `raw` populated, typed/normalized
    fields left `None`. app.extraction.normalize.normalize_candidate() is
    what fills in the typed fields; patterns never parse dates/times or
    resolve URLs themselves, so that logic lives in exactly one place."""

    name: str

    def extract(
        self, response: FetchResponse, config: SiteConfiguration
    ) -> list[EventCandidate]: ...
