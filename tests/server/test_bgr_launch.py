"""Numeric host catalog matching without server or native process setup."""

import pytest

from omnigent.server.bgr_launch import _speed_matches


@pytest.mark.parametrize(
    "advertised,selected,expected",
    [
        (None, None, True),
        (1.0, "1", True),
        (2, "2.0", True),
        ("1.25", "1.250", True),
        (1.25, "1", False),
        (1.0, None, False),
        (None, "1", False),
        (1.0, "invalid", False),
        (True, "1", False),
        ({}, "1", False),
        (float("inf"), "inf", False),
        (float("nan"), "nan", False),
        (0, "0", False),
        (-1, "-1", False),
    ],
)
def test_speed_matches_numeric_value_only(advertised, selected, expected):
    assert _speed_matches(advertised, selected) is expected
