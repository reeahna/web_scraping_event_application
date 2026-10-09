"""Phase 10: the per-site schedule config schema (pure)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.schedule import ScheduleConfig, parse_schedule_config


def test_defaults_are_conservative():
    cfg = ScheduleConfig()
    assert cfg.enabled is True
    assert cfg.interval_minutes == 1440


def test_interval_is_floored():
    with pytest.raises(ValidationError):
        ScheduleConfig(interval_minutes=5)  # below the 15-minute floor


def test_backoff_is_bounded_exponential():
    cfg = ScheduleConfig(retry_backoff_seconds=60, retry_backoff_max_seconds=300)
    assert cfg.backoff_for_attempt(1) == 60
    assert cfg.backoff_for_attempt(2) == 120
    assert cfg.backoff_for_attempt(3) == 240
    assert cfg.backoff_for_attempt(4) == 300  # capped
    assert cfg.backoff_for_attempt(10) == 300


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        ScheduleConfig.model_validate({"interval_minutes": 60, "command": "rm -rf"})


def test_parse_absent_config_is_none_but_invalid_raises():
    assert parse_schedule_config(None) is None
    assert parse_schedule_config({}) is None
    with pytest.raises(ValidationError):
        parse_schedule_config({"interval_minutes": 1})
