"""Reading every active event in bounded batches, for the daily clean-up sweeps.

The home-town and attendability sweeps check every active, unarchived event.
Iterating one query over all of them makes SQLAlchemy buffer every row and
build every Event object before the first one is checked, so the sweep's peak
memory grows with the size of the events table. On a 512 MB instance that the
web process and the scheduler share, two such sweeps starting together after a
deploy were enough to get the instance killed.

`active_event_batches` reads the same events in id order, a fixed number at a
time (keyset pagination on the primary key, so no row is skipped or read twice
even when a sweep deactivates events as it goes). Once a batch is done the
caller drops it, and the session only holds weak references to unmodified
objects, so at most about one batch of events is in memory at a time.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from sqlalchemy.orm import Session

# Large enough that a 50,000-event table takes about 100 short queries, small
# enough that one batch of Event objects (descriptions included) stays at a few
# megabytes.
SWEEP_BATCH_SIZE = 500


def active_event_batches(
    db: Session, *, batch_size: int | None = None
) -> Iterator[list[Any]]:
    """Every active, unarchived event, in id order, as lists of at most
    `batch_size` (SWEEP_BATCH_SIZE when not given). Each batch is a fresh
    query that starts after the last id of the previous one, so the caller may
    commit (and so expire the batch) between batches."""
    from app.models.event import Event

    if batch_size is None:
        batch_size = SWEEP_BATCH_SIZE
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    last_id = 0
    while True:
        batch = (
            db.query(Event)
            .filter(
                Event.is_active.is_(True),
                Event.archived_at.is_(None),
                Event.id > last_id,
            )
            .order_by(Event.id)
            .limit(batch_size)
            .all()
        )
        if not batch:
            return
        # Read before handing the batch over: a commit by the caller expires
        # these objects, and reading an expired id would cost a query each.
        last_id = batch[-1].id
        yield batch
        if len(batch) < batch_size:
            return
