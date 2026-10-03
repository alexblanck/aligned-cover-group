"""Edge cases of the alignment math that are awkward to reach end to end.

Most behavior is covered by the simulated scenarios in test_room.py.
"""

import pytest

from custom_components.aligned_cover_group.alignment import (
    Trip,
    matched_roll_group,
)
from custom_components.aligned_cover_group.roll_profile import RollProfile

from .common import SHADES, SPEED

GROUP = matched_roll_group(
    (
        (config["entity_id"], config["closed_height"], config["open_height"])
        for config in SHADES
    ),
    travel_time_s=max(c["open_height"] - c["closed_height"] for c in SHADES) / SPEED,
)
HIGH_SILL, LOW_SILL = GROUP.shades


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
        RollProfile(closed, opened, halfway)


def test_straight_curves_are_valid() -> None:
    # Its midpoint is computed the same way as the highest allowed halfway
    # height, so floating point can't push it over.
    RollProfile.straight(14.278, 84.143)


def test_views_reach_past_the_measured_shade() -> None:
    rollers = RollProfile(17.875, 125.125, 67.125)
    view = rollers.view(10, 140)
    assert view.closed_pct < 0 < 100 < view.open_pct
    assert view.closed_height == pytest.approx(10)
    assert view.open_height == pytest.approx(140)

    # A strongly curved profile flattens out at 37.5, as if the roll ran out of fabric.
    with pytest.raises(ValueError):
        RollProfile(40, 100, 60).view(30, 70)


INVERSE_PROFILES = {
    "straight": RollProfile.straight(12, 84),
    "living room": RollProfile(17.875, 125.125, 67.125),
    "nearly the quarter limit": RollProfile(12, 84, 30.01),
}


@pytest.mark.parametrize("profile", INVERSE_PROFILES.values(), ids=INVERSE_PROFILES)
def test_height_and_position_are_inverses(profile: RollProfile) -> None:
    span = profile.open_height - profile.closed_height
    for step in range(101):
        assert profile.position_pct_at(profile.height_at(step)) == pytest.approx(step)
        height = profile.closed_height + span * step / 100
        assert profile.height_at(profile.position_pct_at(height)) == pytest.approx(
            height
        )


@pytest.mark.parametrize("profile", INVERSE_PROFILES.values(), ids=INVERSE_PROFILES)
def test_views_clamp_to_their_ends(profile: RollProfile) -> None:
    view = profile.view(profile.closed_height, profile.open_height)
    assert view.height_at(-20) == pytest.approx(profile.closed_height)
    assert view.height_at(130) == pytest.approx(profile.open_height)
    assert view.position_pct_at(profile.closed_height - 5) == pytest.approx(0)
    assert view.position_pct_at(profile.open_height + 5) == pytest.approx(100)
