"""Unit tests for TravelCalculator's position estimation.

These specifically cover the rounding behavior of `_calculate_position()`:
short travel impulses (e.g. the default 0.3s `tilt_time_blind` pulse) used to
be truncated towards the starting position via `int()`, producing a small,
one-sided estimation bias after brief tilt commands. `_calculate_position()`
now rounds to the nearest percent instead.
"""

from __future__ import annotations

from unittest.mock import patch

from custom_components.becker.travelcalculator import TravelCalculator, TravelStatus

TIME_PATCH_TARGET = "custom_components.becker.travelcalculator.time.time"


def _travel_calculator(
    travel_time_down: float = 100.0, travel_time_up: float = 100.0
) -> TravelCalculator:
    return TravelCalculator(travel_time_down=travel_time_down, travel_time_up=travel_time_up)


def test_calculate_position_rounds_up_instead_of_truncating_down() -> None:
    """0.6% progress must round to 1%, not be truncated to 0% by int()."""
    tc = _travel_calculator()
    with patch(TIME_PATCH_TARGET, return_value=0.0):
        tc.set_position(0)
        tc.start_travel(100)

    with patch(TIME_PATCH_TARGET, return_value=0.6):
        assert tc.current_position() == 1


def test_calculate_position_rounds_down_below_half_percent() -> None:
    """0.4% progress correctly rounds down to 0%, same as before the fix."""
    tc = _travel_calculator()
    with patch(TIME_PATCH_TARGET, return_value=0.0):
        tc.set_position(0)
        tc.start_travel(100)

    with patch(TIME_PATCH_TARGET, return_value=0.4):
        assert tc.current_position() == 0


def test_calculate_position_rounding_applies_symmetrically_when_traveling_up() -> None:
    """The same nearest-percent rounding applies for upward travel."""
    tc = _travel_calculator()
    with patch(TIME_PATCH_TARGET, return_value=0.0):
        tc.set_position(100)
        tc.start_travel(0)

    with patch(TIME_PATCH_TARGET, return_value=0.4):
        assert tc.current_position() == 100


def test_calculate_position_reaches_target_once_travel_time_elapsed() -> None:
    """Once the full calculated travel time has passed, the exact target is returned."""
    tc = _travel_calculator(travel_time_down=10.0, travel_time_up=10.0)
    with patch(TIME_PATCH_TARGET, return_value=0.0):
        tc.set_position(0)
        tc.start_travel(100)

    with patch(TIME_PATCH_TARGET, return_value=10.1):
        assert tc.current_position() == 100


def test_stop_freezes_the_rounded_estimate_as_confirmed_position() -> None:
    """Stopping mid-travel freezes the rounded estimate, not a truncated one."""
    tc = _travel_calculator()
    with patch(TIME_PATCH_TARGET, return_value=0.0):
        tc.set_position(0)
        tc.start_travel(100)

    with patch(TIME_PATCH_TARGET, return_value=0.6):
        tc.stop()

    assert tc.current_position() == 1
    assert tc.travel_direction == TravelStatus.STOPPED
    assert tc.position_reached()
