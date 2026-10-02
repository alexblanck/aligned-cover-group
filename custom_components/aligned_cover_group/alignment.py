"""Hemline alignment and motion planning.

Pure math with no Home Assistant imports so it can be unit tested directly.

Naming: `*_pct` values are positions in whole percents following Home
Assistant's convention (0 fully closed, 100 fully open). `*_height` values are
hemline heights: the height of a shade's bottom edge, in whatever unit the user
configured (e.g. inches from the floor).
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum

# A shade counts as aligned when its hemline is within this fraction of the
# group's range from the shared height. Whole-percent positions alone can put a
# shade up to 0.5% of its span off, so this leaves headroom while staying small.
ALIGN_TOLERANCE_FRACTION = 0.01


class Direction(StrEnum):
    """Direction of travel."""

    OPENING = "opening"
    CLOSING = "closing"


def roll_curvature(
    closed_height: float, open_height: float, halfway_height: float
) -> float:
    """Roll curvature from a shade's hemline height at 50%.

    See `Shade`. Zero means the hemline moves in a straight line with position.
    """
    span = open_height - closed_height
    halfway_drop = open_height - halfway_height
    # From drop(t) = t - c t^2 at half and all of the shade's turns.
    extra = 2 * (2 * halfway_drop - span)
    return extra / (span + extra) ** 2


def halfway_height_range(
    closed_height: float, open_height: float
) -> tuple[float, float]:
    """Halfway heights (exclusive low, inclusive high) a roller can have.

    The highest is the straight-line midpoint; below about a quarter of the way
    up the curve would turn back on itself.
    """
    span = open_height - closed_height
    return open_height - 3 * span / 4, open_height - span / 2


@dataclass(frozen=True)
class Shade:
    """Geometry and speed of one shade.

    A roller shade's position counts motor turns, not height: the roll is
    fattest when open, so a turn near the top lowers more fabric than one near
    the bottom. Turns are measured from fully open in units of fabric length at
    the top of the roll, so `turns` turns lower the hemline by
    `turns - roll_curvature * turns**2`. With no curvature, turns are just
    height. Turns change at a constant rate while the motor runs.
    """

    entity_id: str
    closed_height: float
    open_height: float
    travel_time_s: float
    roll_curvature: float = 0.0

    @property
    def span(self) -> float:
        """Distance the hemline travels from closed to open."""
        return self.open_height - self.closed_height

    @property
    def full_turns(self) -> float:
        """Turns from fully open to fully closed."""
        return self._turns_for_drop(self.span)

    @property
    def turn_speed(self) -> float:
        """Turns per second while moving."""
        return self.full_turns / self.travel_time_s

    def turns_down(self, position_pct: float) -> float:
        """Turns from fully open at a shade position."""
        return (1 - position_pct / 100) * self.full_turns

    def turns_down_at(self, hemline_height: float) -> float:
        """Turns from fully open that put the hemline at `hemline_height`."""
        return self._turns_for_drop(self.open_height - self.clamp(hemline_height))

    def hemline_height(self, position_pct: int) -> float:
        """Hemline height at a shade position."""
        turns = self.turns_down(position_pct)
        return self.open_height - (turns - self.roll_curvature * turns**2)

    def clamp(self, hemline_height: float) -> float:
        """The closest hemline height this shade can reach."""
        return min(max(hemline_height, self.closed_height), self.open_height)

    def position_pct_for(self, hemline_height: float) -> int:
        """Shade position that puts the hemline closest to `hemline_height`."""
        turns = self.turns_down_at(hemline_height)
        position_pct = (1 - turns / self.full_turns) * 100
        return round(min(100.0, max(0.0, position_pct)))

    def _turns_for_drop(self, drop: float) -> float:
        if self.roll_curvature == 0:
            return drop
        c = self.roll_curvature
        return (1 - math.sqrt(max(0.0, 1 - 4 * c * drop))) / (2 * c)


@dataclass(frozen=True)
class Trip:
    """A planned shade movement: where it goes and when it starts.

    `needs_command` is False when a Pico press already sends the shade to its
    target, so no `set_position` command is needed.
    """

    shade: Shade
    from_pct: int
    target_pct: int
    delay_s: float = 0.0
    needs_command: bool = True

    @property
    def direction(self) -> Direction:
        """Which way the shade moves."""
        if self.target_pct > self.from_pct:
            return Direction.OPENING
        return Direction.CLOSING

    @property
    def from_height(self) -> float:
        """Hemline height where the shade starts."""
        return self.shade.hemline_height(self.from_pct)

    @property
    def arrival_s(self) -> float:
        """Seconds from the start of the plan until the shade arrives."""
        travel_pct = abs(self.target_pct - self.from_pct)
        return self.delay_s + travel_pct / 100 * self.shade.travel_time_s


@dataclass(frozen=True)
class Plan:
    """How to move the group to a target hemline height.

    `trips` has one entry per shade that moves, sorted by start delay, plus
    "hold" trips (start equals target) for shades still heading to an earlier
    target. If `pico` is set, press that Pico button first: it starts every
    paired shade at once toward the endpoint, and only trips that stop short of
    it need a command. Pico plans never need holds: a held shade partway would
    have blocked the Pico, and one at the endpoint is carried there anyway.
    `pico_blocker` says why an available Pico wasn't used.
    """

    pico: Direction | None
    trips: tuple[Trip, ...]
    pico_blocker: str | None = None

    @property
    def duration_s(self) -> float:
        """Seconds until every shade has arrived."""
        return max((trip.arrival_s for trip in self.trips), default=0.0)


class AlignmentGroup:
    """A set of shades whose hemlines are kept aligned.

    The group's own position runs from the lowest closed height (0%) to the
    highest open height (100%).
    """

    def __init__(self, shades: Iterable[Shade]) -> None:
        """Initialize the group."""
        self.shades = tuple(shades)
        self._entity_ids = {shade.entity_id for shade in self.shades}
        self.closed_height = min(shade.closed_height for shade in self.shades)
        self.open_height = max(shade.open_height for shade in self.shades)
        self.height_tolerance = (
            self.open_height - self.closed_height
        ) * ALIGN_TOLERANCE_FRACTION

    def hemline_height_for(self, position_pct: int) -> float:
        """Hemline height for a group position."""
        span = self.open_height - self.closed_height
        return self.closed_height + position_pct / 100 * span

    def position_pct_for(self, hemline_height: float) -> int:
        """Group position for a hemline height."""
        span = self.open_height - self.closed_height
        position_pct = (hemline_height - self.closed_height) / span * 100
        return round(min(100.0, max(0.0, position_pct)))

    def common_hemline_height(
        self, positions_pct_by_id: Mapping[str, int]
    ) -> float | None:
        """The group hemline height that would put every shade where it is.

        None if there isn't one, meaning the shades are misaligned.
        """
        height = self._candidate_hemline_height(positions_pct_by_id)
        if height is None:
            return None
        for shade in self._shades_for(positions_pct_by_id.keys()):
            actual_height = shade.hemline_height(positions_pct_by_id[shade.entity_id])
            if abs(actual_height - shade.clamp(height)) > self.height_tolerance:
                return None
        return height

    def _candidate_hemline_height(
        self, positions_pct_by_id: Mapping[str, int]
    ) -> float | None:
        shades = self._shades_for(positions_pct_by_id.keys())
        partway = [
            shade.hemline_height(positions_pct_by_id[shade.entity_id])
            for shade in shades
            if 0 < positions_pct_by_id[shade.entity_id] < 100
        ]
        if partway:
            return sum(partway) / len(partway)
        if all(positions_pct_by_id[shade.entity_id] <= 0 for shade in shades):
            return self.closed_height
        if all(positions_pct_by_id[shade.entity_id] >= 100 for shade in shades):
            return self.open_height
        return None

    def current_group_position_pct(
        self, positions_pct_by_id: Mapping[str, int]
    ) -> int | None:
        """Group position to report for the given shade positions."""
        shades = self._shades_for(positions_pct_by_id.keys())
        if not shades:
            return None
        height = self.common_hemline_height(positions_pct_by_id)
        if height is None:
            height = sum(
                shade.hemline_height(positions_pct_by_id[shade.entity_id])
                for shade in shades
            ) / len(shades)
        return self.position_pct_for(height)

    def plan(
        self,
        positions_pct_by_id: Mapping[str, int],
        target_pct: int,
        pico_available: bool,
        moving_entity_ids: Collection[str] = (),
    ) -> Plan:
        """Plan the trips that bring the group to `target_pct`.

        `moving_entity_ids` are shades that may still be heading somewhere
        else; any already at their new target get a trip that holds them there.
        """
        target_height = self.hemline_height_for(target_pct)
        trips: list[Trip] = []
        holds: list[Trip] = []
        for shade in self._shades_for(positions_pct_by_id.keys()):
            trip = Trip(
                shade,
                positions_pct_by_id[shade.entity_id],
                shade.position_pct_for(target_height),
            )
            if trip.from_pct != trip.target_pct:
                trips.append(trip)
            elif shade.entity_id in moving_entity_ids:
                holds.append(trip)
        if not trips:
            return Plan(pico=None, trips=tuple(holds))
        directions = {trip.direction for trip in trips}
        direction = directions.pop() if len(directions) == 1 else None

        blocker = None
        if pico_available:
            if direction is None:
                blocker = "shades are moving in different directions"
            else:
                blocker = self._pico_blocker(positions_pct_by_id, direction, len(trips))
                if blocker is None:
                    return _pico_plan(direction, trips)

        staggered = [
            staggered_trip
            for direction_ in Direction
            for staggered_trip in _staggered(
                [trip for trip in trips if trip.direction is direction_]
            )
        ]
        staggered.sort(key=lambda trip: trip.delay_s)
        return Plan(
            pico=None,
            trips=(*holds, *staggered),
            pico_blocker=blocker,
        )

    def _pico_blocker(
        self,
        positions_pct_by_id: Mapping[str, int],
        direction: Direction,
        moving_count: int,
    ) -> str | None:
        """Why a Pico press can't be used, or None if it's safe.

        The Pico moves every paired shade that isn't already at the endpoint,
        so all of those must need to move and share a hemline height (and all
        positions must be known).
        """
        if len(self._shades_for(positions_pct_by_id.keys())) != len(self.shades):
            return "some shade positions are unknown"
        endpoint_pct = 100 if direction is Direction.OPENING else 0
        heights = [
            shade.hemline_height(positions_pct_by_id[shade.entity_id])
            for shade in self.shades
            if positions_pct_by_id[shade.entity_id] != endpoint_pct
        ]
        if len(heights) != moving_count:
            return "it would move a shade that is already in place"
        shared_height = sum(heights) / len(heights)
        if any(
            abs(height - shared_height) > self.height_tolerance for height in heights
        ):
            return (
                f"shades start from different hemline heights "
                f"({min(heights):.1f} to {max(heights):.1f})"
            )
        return None

    def _shades_for(self, entity_ids: Collection[str]) -> list[Shade]:
        """The group's shades with these entity ids, in group order."""
        if unexpected := set(entity_ids) - self._entity_ids:
            raise ValueError(f"Shades not in the group: {unexpected}")
        return [shade for shade in self.shades if shade.entity_id in entity_ids]


def _staggered(trips: list[Trip]) -> list[Trip]:
    """Delay trips in one direction so each starts when the leader reaches it.

    The leader is the shade furthest from the target.
    """
    if not trips:
        return []
    sign = 1 if trips[0].direction is Direction.OPENING else -1
    leader = min(trips, key=lambda trip: sign * trip.from_height)
    return [
        replace(
            trip,
            delay_s=abs(
                leader.shade.turns_down_at(trip.from_height)
                - leader.shade.turns_down(leader.from_pct)
            )
            / leader.shade.turn_speed,
        )
        for trip in trips
    ]


def _pico_plan(direction: Direction, trips: list[Trip]) -> Plan:
    """The Pico starts every trip; only those stopping short need a command."""
    endpoint_pct = 100 if direction is Direction.OPENING else 0
    return Plan(
        pico=direction,
        trips=tuple(
            replace(trip, needs_command=trip.target_pct != endpoint_pct)
            for trip in trips
        ),
    )
