"""EventDeduplicator.

Cross-run/cross-source "possible duplicate" flagging is handled entirely by
app.services.fingerprints (reused unmodified, see its module for the
required duplicate_status downgrade-protection fix) once a candidate is
persisted as an Event row. This module only handles *within-run*
collapsing — candidates from the same run sharing a fingerprint are never
inserted twice — using the exact same precedence fingerprints.py's
event_fingerprint() uses for a persisted Event, so a within-run fingerprint
and the fingerprint the row gets once persisted always agree:

1. website + external source ID
2. website + normalized canonical URL
3. composite fingerprint (normalized title + date + time + venue + city)
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.extraction.types import EventCandidate
from app.services.fingerprints import normalize_text, normalize_url


def candidate_fingerprint(
    candidate: EventCandidate, *, website_id: int, city_id: int | None
) -> str:
    if candidate.external_source_id:
        identity = f"external|{website_id}|{candidate.external_source_id.strip()}"
    elif normalize_url(candidate.canonical_url):
        identity = f"url|{normalize_url(candidate.canonical_url)}"
    else:
        date_part = candidate.start_date.isoformat() if candidate.start_date else ""
        time_part = candidate.start_time.isoformat() if candidate.start_time else ""
        identity = "|".join(
            (
                "composite",
                normalize_text(candidate.title),
                date_part,
                time_part,
                normalize_text(candidate.venue),
                str(city_id or ""),
            )
        )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DedupOutcome:
    kept: tuple[EventCandidate, ...]
    duplicates_skipped: int


def dedupe_within_run(
    candidates: list[EventCandidate], *, website_id: int, city_id: int | None
) -> DedupOutcome:
    """Collapses candidates sharing a fingerprint, deterministically keeping
    the FIRST occurrence in the given (already deterministic extraction)
    order — never an unordered dict/set iteration decides which survives."""
    seen: dict[str, EventCandidate] = {}
    duplicates = 0
    for candidate in candidates:
        fingerprint = candidate_fingerprint(candidate, website_id=website_id, city_id=city_id)
        if fingerprint in seen:
            duplicates += 1
            continue
        seen[fingerprint] = candidate
    return DedupOutcome(kept=tuple(seen.values()), duplicates_skipped=duplicates)
