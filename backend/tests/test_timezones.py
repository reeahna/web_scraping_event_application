"""Timezone validation and DST-safety warnings (app.core.timezones)."""

import pytest

from app.core.timezones import (
    dst_warning,
    is_fixed_offset_abbreviation,
    is_valid_timezone,
    observes_dst,
)


def test_est_is_flagged_as_a_fixed_offset_abbreviation():
    # EST is a recognised zoneinfo name but a constant -05:00 that ignores DST.
    assert is_valid_timezone("EST")
    assert not observes_dst("EST")
    assert is_fixed_offset_abbreviation("EST")
    warning = dst_warning("EST")
    assert warning is not None
    assert "daylight saving" in warning.lower()


def test_indianapolis_is_a_valid_dst_observing_iana_zone():
    assert is_valid_timezone("America/Indiana/Indianapolis")
    assert observes_dst("America/Indiana/Indianapolis")
    assert not is_fixed_offset_abbreviation("America/Indiana/Indianapolis")
    assert dst_warning("America/Indiana/Indianapolis") is None


@pytest.mark.parametrize("name", ["EST", "MST", "HST"])
def test_bare_dst_less_abbreviations_warn(name):
    assert dst_warning(name) is not None


@pytest.mark.parametrize(
    "name", ["UTC", "GMT", "America/New_York", "America/Phoenix", "Europe/Paris"]
)
def test_regional_and_utc_names_do_not_warn(name):
    # America/Phoenix genuinely never observes DST but is a proper regional
    # name, so it must not be flagged; UTC/GMT are legitimately fixed.
    assert is_valid_timezone(name)
    assert dst_warning(name) is None


def test_unknown_and_empty_names():
    assert not is_valid_timezone("Not/A_Zone")
    assert dst_warning("Not/A_Zone") is None
    assert dst_warning(None) is None
    assert dst_warning("") is None
