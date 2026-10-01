"""Edge cases of the alignment math that are awkward to reach end to end.

Most behavior is covered by the simulated scenarios in test_room.py.
"""

import pytest

from custom_components.aligned_cover_group.alignment import (
    Group,
    Shade,
    Trip,
)

# Same top, different sills, same speed (2 units/s).
HIGH_SILL = Shade("cover.high_sill", closed_height=24, open_height=84, travel_time_s=30)
LOW_SILL = Shade("cover.low_sill", closed_height=12, open_height=84, travel_time_s=36)
GROUP = Group([HIGH_SILL, LOW_SILL])


def positions_pct(high: int, low: int) -> dict[str, int]:
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
def test_reported_position_when_aligned(high: int, low: int, expected: int) -> None:
    assert GROUP.common_hemline_height(positions_pct(high, low)) is not None
    assert GROUP.reported_position_pct(positions_pct(high, low)) == expected


def test_reported_position_falls_back_to_average_hemline() -> None:
    # Hemlines 54 and 48: misaligned, average 51 -> (51 - 12) / 72.
    assert GROUP.common_hemline_height(positions_pct(50, 50)) is None
    assert GROUP.reported_position_pct(positions_pct(50, 50)) == 54


def test_pico_skipped_when_a_shade_it_would_move_should_stay() -> None:
    # Aligned at 48; target 48.33 moves the high-sill shade (40 -> 41) but not
    # the low-sill one (50.46 -> 50), so a Pico press would wrongly move it.
    plan = GROUP.plan(positions_pct(40, 50), 48.33, pico_available=True)
    assert plan.pico is None


def test_pico_skipped_when_a_position_is_unknown() -> None:
    plan = GROUP.plan(
        {LOW_SILL.entity_id: 100}, GROUP.closed_height, pico_available=True
    )
    assert plan.pico is None
    assert plan.trips == (Trip(LOW_SILL, from_pct=100, target_pct=0),)


def test_mixed_directions() -> None:
    # Misaligned: hemlines 54 and 48, target 50 -> one up, one down.
    plan = GROUP.plan(positions_pct(50, 50), 51, pico_available=True)
    assert plan.pico is None
    assert plan.direction is None
    assert {trip.delay_s for trip in plan.trips} == {0.0}


def test_positions_for_shades_outside_the_group_are_rejected() -> None:
    with pytest.raises(ValueError, match="cover.stranger"):
        GROUP.reported_position_pct({**positions_pct(0, 0), "cover.stranger": 50})
