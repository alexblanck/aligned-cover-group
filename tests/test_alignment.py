"""Edge cases of the alignment math that are awkward to reach end to end.

Most behavior is covered by the simulated scenarios in test_room.py.
"""

import pytest

from custom_components.aligned_cover_group.alignment import (
    AlignmentGroup,
    HemlineCurve,
    Shade,
    Trip,
)

from .common import SHADES, SPEED

HIGH_SILL, LOW_SILL = (
    Shade(
        config["entity_id"],
        HemlineCurve.straight(config["closed_height"], config["open_height"]),
        travel_time_s=(config["open_height"] - config["closed_height"]) / SPEED,
    )
    for config in SHADES
)
GROUP = AlignmentGroup([HIGH_SILL, LOW_SILL])


def positions_pct_by_id(high: int, low: int) -> dict[str, int]:
    return {HIGH_SILL.entity_id: high, LOW_SILL.entity_id: low}


@pytest.mark.parametrize(
    ("high", "low", "expected"),
    [
        (0, 0, 0),  # all closed
        (100, 100, 100),  # all open
        (40, 50, 50),  # aligned mid-travel
        (0, 8, 8),  # high-sill shade clamped closed below its sill
    ],
)
def test_group_position_when_aligned(high: int, low: int, expected: int) -> None:
    assert GROUP.common_hemline_height(positions_pct_by_id(high, low)) is not None
    assert GROUP.current_group_position_pct(positions_pct_by_id(high, low)) == expected


def test_group_position_falls_back_to_average_hemline() -> None:
    # Hemlines 54 and 48: misaligned, average 51 -> (51 - 12) / 72.
    assert GROUP.common_hemline_height(positions_pct_by_id(50, 50)) is None
    assert GROUP.current_group_position_pct(positions_pct_by_id(50, 50)) == 54


def test_pico_skipped_when_a_shade_it_would_move_should_stay() -> None:
    # Aligned within tolerance (hemlines 48.6 and 48.0); going to 51% moves the
    # low-sill shade (50 -> 51) but not the high-sill one (41.2 -> 41), so a
    # Pico press would wrongly move it.
    plan = GROUP.plan(positions_pct_by_id(41, 50), 51, pico_available=True)
    assert plan.pico is None
    assert plan.pico_blocker == "it would move a shade that is already in place"


def test_pico_skipped_when_a_position_is_unknown() -> None:
    plan = GROUP.plan({LOW_SILL.entity_id: 100}, 0, pico_available=True)
    assert plan.pico is None
    assert plan.trips == (Trip(LOW_SILL, from_pct=100, target_pct=0),)


def test_positions_for_shades_outside_the_group_are_rejected() -> None:
    with pytest.raises(ValueError, match="cover.stranger"):
        GROUP.current_group_position_pct(
            {**positions_pct_by_id(0, 0), "cover.stranger": 50}
        )


@pytest.mark.parametrize(
    ("closed", "opened", "halfway"),
    [
        (50, 50, 50),  # no range
        (80, 20, 50),  # upside down
        (12, 84, 48.5),  # above the midpoint: faster near the bottom
        (12, 84, 30),  # a quarter of the way up: stops at the bottom
        (12, 84, 20),  # below a quarter: reverses at the bottom
    ],
)
def test_impossible_curves_are_rejected(
    closed: float, opened: float, halfway: float
) -> None:
    with pytest.raises(ValueError):
        HemlineCurve(closed, opened, halfway)


def test_slices_of_valid_curves_are_valid() -> None:
    # Floating point can put a straight curve's midpoint a hair above the
    # highest allowed halfway height; it must still be accepted.
    HemlineCurve.straight(14.278, 84.143)
    straight = HemlineCurve.straight(12, 84)
    for closed, opened in [(24, 84), (13.1, 83.7), (12.000001, 84)]:
        straight.rescaled_to(closed, opened)
    rollers = HemlineCurve(17.875, 125.125, 67.125)
    rollers.rescaled_to(49.75, 125.125)


INVERSE_CURVES = {
    "straight": HemlineCurve.straight(12, 84),
    "living room": HemlineCurve(17.875, 125.125, 67.125),
    "nearly the quarter limit": HemlineCurve(12, 84, 30.01),
}


@pytest.mark.parametrize("curve", INVERSE_CURVES.values(), ids=INVERSE_CURVES)
def test_height_and_position_are_inverses(curve: HemlineCurve) -> None:
    span = curve.open_height - curve.closed_height
    for step in range(101):
        assert curve.position_pct_at(curve.height_at(step)) == pytest.approx(step)
        height = curve.closed_height + span * step / 100
        assert curve.height_at(curve.position_pct_at(height)) == pytest.approx(height)


@pytest.mark.parametrize("curve", INVERSE_CURVES.values(), ids=INVERSE_CURVES)
def test_height_and_position_clamp_to_the_curve(curve: HemlineCurve) -> None:
    assert curve.height_at(-20) == curve.closed_height
    assert curve.height_at(130) == curve.open_height
    assert curve.position_pct_at(curve.closed_height - 5) == 0
    assert curve.position_pct_at(curve.open_height + 5) == pytest.approx(100)
