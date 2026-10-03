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

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum

from .roll_profile import RollProfile, RollProfileView

# A shade counts as aligned when its hemline is within this fraction of the
# group's range from the shared height. Whole-percent positions alone can put a
# shade up to 0.5% of its span off, so this leaves headroom while staying small.
ALIGN_TOLERANCE_FRACTION = 0.01


class Direction(StrEnum):
    """Direction of travel."""

    OPENING = "opening"
    CLOSING = "closing"


@dataclass(frozen=True)
class Shade:
    """One shade: its view of a roll profile (between its limits), and how long
    a full travel takes. Positions change at a constant rate while moving.
    """

    entity_id: str
    view: RollProfileView
    travel_time_s: float

    @property
    def closed_height(self) -> float:
        """Hemline height when fully closed."""
        return self.view.closed_height

    @property
    def open_height(self) -> float:
        """Hemline height when fully open."""
        return self.view.open_height

    def hemline_height(self, position_pct: float) -> float:
        """Hemline height at a shade position."""
        return self.view.height_at(position_pct)

    def clamp(self, hemline_height: float) -> float:
        """The closest hemline height this shade can reach."""
        return self.view.clamp(hemline_height)

    def exact_position_pct_for(self, hemline_height: float) -> float:
        """Unrounded position in percent that puts the hemline at `hemline_height`."""
        return self.view.position_pct_at(hemline_height)

    def position_pct_for(self, hemline_height: float) -> int:
        """Shade position that puts the hemline closest to `hemline_height`."""
        return round(self.exact_position_pct_for(hemline_height))


def matched_roll_group(
    shades: Iterable[tuple[str, float, float]],
    travel_time_s: float,
    halfway_height: float | None = None,
) -> AlignmentGroup:
    """A group of shades whose rolls match at every hemline height.

    `shades` are (entity_id, closed_height, open_height). The tallest shade
    (longest range) is the measured one: `travel_time_s` is its full travel,
    and `halfway_height` its hemline at 50% (without it, height is
    proportional to position). Every shade, and the group's own position, sees
    a view of its roll profile, and a shade's travel time is its view's share
    of the tallest one's. Raises ValueError if the profile can't reach every
    shade.
    """
    shades = list(shades)
    _, tallest_closed, tallest_open = max(shades, key=lambda shade: shade[2] - shade[1])
    profile = (
        RollProfile.straight(tallest_closed, tallest_open)
        if halfway_height is None
        else RollProfile(tallest_closed, tallest_open, halfway_height)
    )
    views = {
        entity_id: profile.view(closed, opened) for entity_id, closed, opened in shades
    }
    return AlignmentGroup(
        (
            Shade(entity_id, view, view.size_pct / 100 * travel_time_s)
            for entity_id, view in views.items()
        ),
        profile.view(
            min(closed for _, closed, _ in shades),
            max(opened for _, _, opened in shades),
        ),
    )


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

    The group's own position is a view of the shades' shared roll profile,
    from the lowest closed height (0%) to the highest open height (100%). On
    the measured (tallest) shade's profile, that makes the group's position
    match the tallest shade's, and identical shades match the group exactly.
    """

    def __init__(self, shades: Iterable[Shade], view: RollProfileView) -> None:
        """Initialize the group."""
        self.shades = tuple(shades)
        self._entity_ids = {shade.entity_id for shade in self.shades}
        self.view = view
        self.closed_height = view.closed_height
        self.open_height = view.open_height
        self.height_tolerance = (
            self.open_height - self.closed_height
        ) * ALIGN_TOLERANCE_FRACTION

    def hemline_height_for(self, position_pct: int) -> float:
        """Hemline height for a group position."""
        return self.view.height_at(position_pct)

    def position_pct_for(self, hemline_height: float) -> int:
        """Group position for a hemline height."""
        return round(self.view.position_pct_at(hemline_height))

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
