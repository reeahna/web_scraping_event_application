"""Dedicated scheduler process (Phase 10).

This package is the *only* place APScheduler runs. It is never started inside a
FastAPI worker — running it per web worker would give N schedulers racing over
the same sites. Instead it runs as one dedicated process (``python -m
app.scheduler``); a DB leader row is claimed as a safety net so a second
accidental process stays idle.

Durability lives in the database, not in APScheduler's own jobstore: each
site's next run is persisted on ``scheduler_job_state`` and per-site locks with
heartbeats survive a crash, so a restart reconciles and resumes without losing
or double-running anything. APScheduler here only drives two recurring ticks —
a leader heartbeat and a due-work dispatcher.
"""

from app.scheduler.runtime import SchedulerRuntime

__all__ = ["SchedulerRuntime"]
