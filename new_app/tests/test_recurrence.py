"""Phase 8G: the shared recurrence expander."""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from app.extraction.recurrence import expand_recurrence, occurrence_identity
from app.schemas.recurrence import RecurrenceBounds, RecurrenceOccurrence, RecurrenceSpec

REF = date(2026, 1, 1)
FP = "composite|fair|2026-01-05"


def _expand(spec, start, **kw):
    return expand_recurrence(
        spec, parent_start=start, parent_fingerprint=FP, reference_date=REF, **kw
    )


def test_parent_only_emits_a_single_occurrence():
    spec = RecurrenceSpec(mode="parent_only")
    result = _expand(spec, datetime(2026, 3, 1, 19, 0))
    assert len(result.occurrences) == 1
    assert result.occurrences[0].start_date == date(2026, 3, 1)


def test_bounded_weekly_expansion_with_count():
    spec = RecurrenceSpec(mode="bounded_expand", rrule="FREQ=WEEKLY;COUNT=5")
    result = _expand(spec, datetime(2026, 3, 2, 19, 0))  # a Monday
    assert len(result.occurrences) == 5
    # Wall-clock time is preserved on every occurrence (DST-correct for a local
    # listing): all start at 19:00.
    assert all(o.start_time == time(19, 0) for o in result.occurrences)
    assert [o.start_date for o in result.occurrences] == [
        date(2026, 3, 2), date(2026, 3, 9), date(2026, 3, 16),
        date(2026, 3, 23), date(2026, 3, 30),
    ]


def test_horizon_bounds_an_unbounded_rule():
    spec = RecurrenceSpec(mode="bounded_expand", rrule="FREQ=DAILY")
    result = _expand(spec, datetime(2026, 1, 1, 9, 0), bounds=RecurrenceBounds(horizon_days=10))
    # 10-day horizon from the reference date, inclusive of the reference day.
    assert 1 <= len(result.occurrences) <= 11
    assert max(o.start_date for o in result.occurrences) <= date(2026, 1, 11)


def test_per_parent_cap_truncates_and_warns():
    spec = RecurrenceSpec(mode="bounded_expand", rrule="FREQ=DAILY")
    result = _expand(
        spec, datetime(2026, 1, 1, 9, 0),
        bounds=RecurrenceBounds(horizon_days=400, max_occurrences_per_parent=30),
    )
    assert len(result.occurrences) == 30
    assert result.truncated is True
    assert "recurrence_truncated_per_parent" in result.warnings


def test_subdaily_frequency_is_refused_not_expanded():
    spec = RecurrenceSpec(mode="bounded_expand", rrule="FREQ=HOURLY;COUNT=100")
    result = _expand(spec, datetime(2026, 3, 1, 9, 0))
    assert "recurrence_subdaily_refused" in result.warnings
    assert len(result.occurrences) == 1  # fell back to the parent


def test_exdate_excludes_and_rdate_adds():
    spec = RecurrenceSpec(
        mode="bounded_expand",
        rrule="FREQ=WEEKLY;COUNT=3",
        exdate=["2026-03-09"],
        rdate=["2026-04-15T19:00:00"],
    )
    result = _expand(spec, datetime(2026, 3, 2, 19, 0))
    starts = {o.start_date for o in result.occurrences}
    assert date(2026, 3, 9) not in starts  # excluded
    assert date(2026, 4, 15) in starts  # added


def test_explicit_occurrences_mode_uses_them_verbatim():
    spec = RecurrenceSpec(
        mode="explicit_occurrences",
        occurrences=[
            RecurrenceOccurrence(start="2026-05-01T18:00:00", source_occurrence_id="a"),
            RecurrenceOccurrence(start="2026-05-08T18:00:00", source_occurrence_id="b"),
        ],
    )
    result = _expand(spec, datetime(2026, 5, 1, 18, 0))
    assert len(result.occurrences) == 2
    assert result.occurrences[0].identity == "srcocc|a"


def test_detached_occurrence_overrides_generated_slot():
    spec = RecurrenceSpec(
        mode="bounded_expand",
        rrule="FREQ=WEEKLY;COUNT=3",
        source_parent_id="P1",
        occurrences=[
            RecurrenceOccurrence(
                start="2026-03-09T20:30:00",  # moved later that day
                recurrence_id="2026-03-09T19:00:00",
                title="Moved show",
            )
        ],
    )
    result = _expand(spec, datetime(2026, 3, 2, 19, 0))
    # Still three slots, but the middle one is the detached override.
    assert len(result.occurrences) == 3
    moved = [o for o in result.occurrences if o.start_date == date(2026, 3, 9)]
    assert len(moved) == 1
    assert moved[0].detached is True
    assert moved[0].title == "Moved show"
    assert moved[0].start_time == time(20, 30)


def test_cancelled_occurrence_is_flagged_not_dropped():
    spec = RecurrenceSpec(
        mode="bounded_expand",
        rrule="FREQ=WEEKLY;COUNT=3",
        occurrences=[
            RecurrenceOccurrence(start="2026-03-09T19:00:00", cancelled=True, recurrence_id="x")
        ],
    )
    result = _expand(spec, datetime(2026, 3, 2, 19, 0))
    cancelled = [o for o in result.occurrences if o.cancelled]
    assert len(cancelled) == 1
    assert cancelled[0].start_date == date(2026, 3, 9)
    # The slot is still present — nothing was deleted.
    assert len(result.occurrences) == 3


def test_all_day_recurrence_has_no_times():
    spec = RecurrenceSpec(mode="bounded_expand", rrule="FREQ=DAILY;COUNT=3", all_day=True)
    result = _expand(spec, date(2026, 6, 1))
    assert all(o.all_day and o.start_time is None for o in result.occurrences)


def test_duration_propagates_to_end():
    spec = RecurrenceSpec(mode="bounded_expand", rrule="FREQ=WEEKLY;COUNT=2")
    result = _expand(
        spec, datetime(2026, 3, 2, 19, 0), parent_end=datetime(2026, 3, 2, 21, 30)
    )
    assert result.occurrences[0].end_time == time(21, 30)
    assert result.occurrences[1].end_date == date(2026, 3, 9)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (dict(source_occurrence_id="occ7", recurrence_id="r", normalized_start="2026-03-09"),
         "srcocc|occ7"),
        (dict(source_parent_id="P1", recurrence_id="2026-03-09T19:00:00",
              normalized_start="2026-03-09"),
         "parentrec|P1|2026-03-09T19:00:00"),
        (dict(normalized_start="2026-03-09"), f"fp|{FP}|2026-03-09"),
    ],
)
def test_identity_precedence(kwargs, expected):
    base = dict(parent_fingerprint=FP, source_parent_id=None,
                source_occurrence_id=None, recurrence_id=None)
    base.update(kwargs)
    assert occurrence_identity(**base) == expected


def test_expansion_is_deterministic():
    spec = RecurrenceSpec(mode="bounded_expand", rrule="FREQ=WEEKLY;COUNT=5")
    a = _expand(spec, datetime(2026, 3, 2, 19, 0))
    b = _expand(spec, datetime(2026, 3, 2, 19, 0))
    assert [o.identity for o in a.occurrences] == [o.identity for o in b.occurrences]
