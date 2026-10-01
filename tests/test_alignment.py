"""Edge cases of the alignment math that are awkward to reach end to end.

Most behavior is covered by the simulated scenarios in test_room.py.
"""

import pytest

from custom_components.aligned_cover_group.alignment import (
    AlignmentGroup,
    Shade,
    Trip,
)

from .common import SHADES, SPEED

HIGH_SILL, LOW_SILL = (
    Shade(
        **config,
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
