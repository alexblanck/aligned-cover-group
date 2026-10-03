"""Hemline alignment and motion planning.

Pure math with no Home Assistant imports so it can be unit tested directly.

Naming:

- `*_pct` values are positions in percent, following Home Assistant's
  convention (0 fully closed, 100 fully open). Positions shades report or are
  sent are whole numbers (`int`); positions worked out along a curve are
  fractional (`float`) and only rounded when a shade is commanded.
- `*_height` values are hemline heights: the height of a shade's bottom edge,
  in whatever unit the user measured in.
- `*_s` values are seconds.
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

# Slack, as a fraction of a curve's range, when checking its halfway height:
# slices of a straight curve compute their midpoint with floating-point error.
HALFWAY_TOLERANCE_FRACTION = 1e-9


class Direction(StrEnum):
    """Direction of travel."""

    OPENING = "opening"
    CLOSING = "closing"


def halfway_height_range(
    closed_height: float, open_height: float
) -> tuple[float, float]:
    """Halfway heights (exclusive low, inclusive high) a roller can have.

    Outside this range, the curve through the three heights would describe a
    shade that can't exist, so the measurement must be wrong. Above the
    midpoint, the hemline would speed up as it lowered, as if the roll got
    fatter while unwinding. At or below a quarter of the way up, the hemline
    would slow to a stop, or even reverse, just before reaching closed.
    """
    span = open_height - closed_height
    return closed_height + span / 4, closed_height + span / 2


@dataclass(frozen=True)
class HemlineCurve:
    """How a single shade's hemline height follows its position (0-100%).

    The group's position uses one too, as if the group were a single shade
    covering every shade's range.

    Positions count motor rotation, as on most motorized roller shades,
    including the Lutron Serena shades this was built and tested with. Their
    motors turn at a steady speed, so a position is also a share of the run
    time. Rotation isn't proportional to height, though: the roll is fattest
    when open, so the hemline moves faster near the top. A roll that shrinks
    steadily as it unwinds makes height a quadratic in position, so the curve
    is the quadratic through three points: closed (0%), halfway (50%) and open
    (100%). With the halfway height at the midpoint it's a straight line.
    """

    closed_height: float
    open_height: float
    halfway_height: float

    def __post_init__(self) -> None:
        """Reject curves no shade could follow (see `halfway_height_range`)."""
        if not self.open_height > self.closed_height:
            raise ValueError(
                f"Open height {self.open_height} must be above "
                f"closed height {self.closed_height}"
            )
        low, high = halfway_height_range(self.closed_height, self.open_height)
        slack = self._span * HALFWAY_TOLERANCE_FRACTION
        if not low < self.halfway_height <= high + slack:
            raise ValueError(
                f"Halfway height {self.halfway_height} must be above {low} "
                f"and at most {high}"
            )

    @classmethod
    def straight(cls, closed_height: float, open_height: float) -> HemlineCurve:
        """A curve where height is proportional to position."""
        return cls(closed_height, open_height, (closed_height + open_height) / 2)

    def height_at(self, position_pct: float) -> float:
        """Hemline height at a position, clamped to 0-100%."""
        return self._height_extended(min(max(position_pct, 0.0), 100.0))

    def position_pct_at(self, hemline_height: float) -> float:
        """Unrounded position in percent for a height, clamped to the curve's range."""
        clamped = min(max(hemline_height, self.closed_height), self.open_height)
        return self._position_pct_extended(clamped)

    def rescaled_to(self, closed_height: float, open_height: float) -> HemlineCurve:
        """The same curve, with 0-100% running from `closed_height` to `open_height`.

        For shades whose rolls match at every hemline height: a shade with a
        raised bottom limit or a lower top follows part of this curve, and one
        reaching higher or lower follows it extended beyond its range. Raises
        ValueError if the extended curve can't get there (it would flatten out,
        as if the roll ran out of fabric).
        """
        middle = (
            self._position_pct_extended(closed_height)
            + self._position_pct_extended(open_height)
        ) / 2
        return HemlineCurve(closed_height, open_height, self._height_extended(middle))

    def _height_extended(self, position_pct: float) -> float:
        """Height at a position, extending the curve beyond 0-100%."""
        fraction = position_pct / 100
        rise, bend = self._shape()
        return self.closed_height + self._span * (rise * fraction + bend * fraction**2)

    def _position_pct_extended(self, hemline_height: float) -> float:
        """Position in percent for a height, extending the curve beyond its range."""
        fraction_up = (hemline_height - self.closed_height) / self._span
        rise, bend = self._shape()
        # Solves rise * x + bend * x**2 = fraction_up for x, on the rising side
        # of the curve. With d = rise**2 + 4 * bend * fraction_up, the usual
        # quadratic formula is
        #     x = (-rise + sqrt(d)) / (2 * bend)
        # which divides by bend, zero for a straight line. Multiplying top and
        # bottom by (rise + sqrt(d)) gives this equivalent form, which doesn't,
        # and becomes fraction_up / rise when bend is zero.
        discriminant = rise**2 + 4 * bend * fraction_up
        denominator = rise + math.sqrt(max(discriminant, 0.0))
        if discriminant < 0 or denominator <= 0:
            raise ValueError(f"The curve never reaches {hemline_height}")
        return 100 * 2 * fraction_up / denominator

    @property
    def _span(self) -> float:
        return self.open_height - self.closed_height

    def _shape(self) -> tuple[float, float]:
        """Coefficients of height = closed + span * (rise * x + bend * x**2)."""
        halfway_fraction = (self.halfway_height - self.closed_height) / self._span
        return 4 * halfway_fraction - 1, 2 - 4 * halfway_fraction


def shared_roll_curve(
    ranges: Iterable[tuple[float, float]], halfway_height: float | None
) -> HemlineCurve:
    """The group's curve for shades whose rolls match at every hemline height.

    `ranges` are the shades' (closed, open) heights, and `halfway_height` is
    the tallest one's hemline at 50% (without it, height is proportional to
    position). The tallest shade's curve is extended to cover every shade.
    Raises ValueError if it can't be.
    """
    ranges = list(ranges)
    lowest = min(closed for closed, _ in ranges)
    highest = max(opened for _, opened in ranges)
    if halfway_height is None:
        return HemlineCurve.straight(lowest, highest)
    closed, opened = max(ranges, key=lambda heights: heights[1] - heights[0])
    return HemlineCurve(closed, opened, halfway_height).rescaled_to(lowest, highest)


@dataclass(frozen=True)
class Shade:
    """One shade: how its hemline height follows its position, and how long a
    full travel takes. Positions change at a constant rate while moving.

    `halfway_height` is the hemline at 50%; without it, height is proportional
    to position.
    """

    entity_id: str
    closed_height: float
    open_height: float
    travel_time_s: float
    halfway_height: float | None = None

    @property
    def curve(self) -> HemlineCurve:
        """This shade's hemline height across its own 0-100%."""
        if self.halfway_height is None:
            return HemlineCurve.straight(self.closed_height, self.open_height)
        return HemlineCurve(self.closed_height, self.open_height, self.halfway_height)

    def hemline_height(self, position_pct: float) -> float:
        """Hemline height at a shade position."""
        return self.curve.height_at(position_pct)

    def clamp(self, hemline_height: float) -> float:
        """The closest hemline height this shade can reach."""
        return min(max(hemline_height, self.closed_height), self.open_height)

    def exact_position_pct_for(self, hemline_height: float) -> float:
        """Unrounded position in percent that puts the hemline at `hemline_height`."""
        return self.curve.position_pct_at(hemline_height)

    def position_pct_for(self, hemline_height: float) -> int:
        """Shade position that puts the hemline closest to `hemline_height`."""
        return round(self.exact_position_pct_for(hemline_height))


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
    highest open height (100%) along `curve`. For shades sharing one roll,
    that's the tallest shade's curve, so the group's position matches the
    tallest shade's and identical shades match the group exactly.
    """

    def __init__(
        self, shades: Iterable[Shade], curve: HemlineCurve | None = None
    ) -> None:
        """Initialize the group; without a curve, height is proportional."""
        self.shades = tuple(shades)
        self._entity_ids = {shade.entity_id for shade in self.shades}
        self.closed_height = min(shade.closed_height for shade in self.shades)
        self.open_height = max(shade.open_height for shade in self.shades)
        self.curve = curve or HemlineCurve.straight(
            self.closed_height, self.open_height
        )
        self.height_tolerance = (
            self.open_height - self.closed_height
        ) * ALIGN_TOLERANCE_FRACTION

    def hemline_height_for(self, position_pct: int) -> float:
        """Hemline height for a group position."""
        return self.curve.height_at(position_pct)

    def position_pct_for(self, hemline_height: float) -> int:
        """Group position for a hemline height."""
        return round(self.curve.position_pct_at(hemline_height))

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
            # How long the leader takes to move from where it starts to the
            # follower's hemline: positions change at a constant rate.
            delay_s=abs(
                leader.shade.exact_position_pct_for(trip.from_height) - leader.from_pct
            )
            / 100
            * leader.shade.travel_time_s,
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
