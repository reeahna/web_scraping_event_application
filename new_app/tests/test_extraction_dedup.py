from datetime import date

from app.extraction.dedup import candidate_fingerprint, dedupe_within_run
from app.extraction.types import EventCandidate
from app.services.fingerprints import event_fingerprint


def _candidate(**overrides) -> EventCandidate:
    defaults = dict(
        raw={},
        title="Some Event",
        canonical_url="https://example.com/events/some-event",
        description=None,
        start_date=date(2025, 6, 1),
        start_time=None,
        end_date=None,
        end_time=None,
        timezone=None,
        venue="A Venue",
        address=None,
        image_url=None,
        latitude=None,
        longitude=None,
        source_category=None,
        external_source_id=None,
        field_source_paths={},
        transformation_history=(),
        source_page="https://example.com/events",
        extraction_pattern="json_ld_event",
        warnings=(),
        raw_record_hash="abc123",
    )
    defaults.update(overrides)
    return EventCandidate(**defaults)


def test_duplicate_within_one_run_collapsed():
    a = _candidate()
    b = _candidate(raw_record_hash="different-hash")  # same identity fields
    outcome = dedupe_within_run([a, b], website_id=1, city_id=None)
    assert len(outcome.kept) == 1
    assert outcome.duplicates_skipped == 1
    assert outcome.kept[0] is a  # first occurrence deterministically retained


def test_external_id_precedence_over_url():
    a = _candidate(external_source_id="ext-1", canonical_url="https://example.com/a")
    b = _candidate(
        external_source_id="ext-1", canonical_url="https://example.com/completely-different"
    )
    outcome = dedupe_within_run([a, b], website_id=1, city_id=None)
    assert len(outcome.kept) == 1  # matched by external_source_id despite different URLs


def test_canonical_url_precedence_when_no_external_id():
    # Same host (case-insensitive) and same query params in a different
    # order — normalize_url() canonicalizes both, so these must collide.
    a = _candidate(canonical_url="https://example.com/events/x?a=1&b=2")
    b = _candidate(canonical_url="https://EXAMPLE.com/events/x?b=2&a=1")
    outcome = dedupe_within_run([a, b], website_id=1, city_id=None)
    assert len(outcome.kept) == 1


def test_composite_fingerprint_fallback_when_no_id_or_url():
    a = _candidate(
        canonical_url=None, title="Same Title", start_date=date(2025, 6, 1), venue="Venue"
    )
    b = _candidate(
        canonical_url=None, title="Same Title", start_date=date(2025, 6, 1), venue="Venue"
    )
    c = _candidate(
        canonical_url=None, title="Different Title", start_date=date(2025, 6, 1), venue="Venue"
    )
    outcome = dedupe_within_run([a, b, c], website_id=1, city_id=None)
    assert len(outcome.kept) == 2


def test_deterministic_retained_candidate_is_stable_across_runs():
    a = _candidate()
    b = _candidate(raw_record_hash="other")
    first = dedupe_within_run([a, b], website_id=1, city_id=None)
    second = dedupe_within_run([a, b], website_id=1, city_id=None)
    assert first.kept[0].raw_record_hash == second.kept[0].raw_record_hash


def test_candidate_fingerprint_matches_persisted_event_fingerprint_algorithm():
    """The within-run fingerprint must agree with the fingerprint a real
    persisted Event row would get, so upsert matching and within-run
    dedup never diverge."""
    from app.models.event import Event

    candidate = _candidate(external_source_id=None, canonical_url="https://example.com/events/x")
    fp_from_candidate = candidate_fingerprint(candidate, website_id=7, city_id=None)

    event = Event(
        title=candidate.title,
        canonical_url=candidate.canonical_url,
        source="Test",
        website_id=7,
        start_date=candidate.start_date,
        venue=candidate.venue,
    )
    fp_from_event = event_fingerprint(event)
    assert fp_from_candidate == fp_from_event
