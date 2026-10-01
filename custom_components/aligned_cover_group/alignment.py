"""Hemline alignment and motion planning.

Pure math with no Home Assistant imports so it can be unit tested directly.

Positions follow Home Assistant's convention: 0 is fully closed, 100 is fully
open. A "hemline" is the height of a shade's bottom edge, in whatever unit the
user configured (e.g. inches from the floor).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

# Hemlines within this fraction of the group's range count as aligned.
ALIGN_TOLERANCE = 0.02


class Direction(StrEnum):
    """Direction of travel."""

    OPENING = "opening"
    CLOSING = "closing"


@dataclass(frozen=True)
class Shade:
    """Geometry and speed of one shade."""

    entity_id: str
    closed_height: float
    open_height: float
    travel_time: float

    @property
    def span(self) -> float:
        """Distance the hemline travels from closed to open."""
        return self.open_height - self.closed_height

    @property
    def speed(self) -> float:
        """Hemline speed in height units per second."""
        return self.span / self.travel_time

    def hemline(self, position: float) -> float:
        """Hemline height for a shade position."""
        return self.closed_height + position / 100 * self.span

    def position_for(self, hemline: float) -> int:
        """Shade position that puts the hemline as close to `hemline` as possible."""
        position = (hemline - self.closed_height) / self.span * 100
        return round(min(100.0, max(0.0, position)))


@dataclass(frozen=True)
class Move:
    """Send one shade to a position after a delay."""

    entity_id: str
    position: int
    delay: float


@dataclass(frozen=True)
class Plan:
    """How to move the group to a target hemline.

    If `pico` is set, press that Pico button first; it starts every paired shade
    at once. `moves` then retarget shades that should stop short of the endpoint.
    `trips` describes where every moving shade is headed and when it starts,
    however it is commanded.
    """

    pico: Direction | None
    moves: tuple[Move, ...]
    direction: Direction | None
    duration: float
    trips: tuple[Move, ...] = ()


class Group:
    """A set of shades whose hemlines are kept aligned."""

    def __init__(self, shades: Iterable[Shade]) -> None:
        """Initialize the group."""
        self.shades = tuple(shades)
        self.low = min(shade.closed_height for shade in self.shades)
        self.high = max(shade.open_height for shade in self.shades)
        self.tolerance = (self.high - self.low) * ALIGN_TOLERANCE

    def hemline_for(self, group_position: float) -> float:
        """Hemline height for a group position."""
        return self.low + group_position / 100 * (self.high - self.low)

    def group_position_for(self, hemline: float) -> int:
        """Group position for a hemline height."""
        position = (hemline - self.low) / (self.high - self.low) * 100
        return round(min(100.0, max(0.0, position)))

    def common_hemline(self, positions: Mapping[str, float]) -> float | None:
        """Return the hemline all shades agree on, or None if misaligned.

        A fully closed shade is consistent with any hemline at or below its
        closed height (and likewise for fully open), so shades of different
        sizes still count as aligned when the shorter ones are clamped.
        """
        lower, upper = self.low, self.high
        has_point = False
        for shade in self._known(positions):
            position = positions[shade.entity_id]
            if position <= 0:
                upper = min(upper, shade.closed_height)
            elif position >= 100:
                lower = max(lower, shade.open_height)
            else:
                hemline = shade.hemline(position)
                lower = max(lower, hemline)
                upper = min(upper, hemline)
                has_point = True
        if lower > upper + self.tolerance:
            return None
        if not has_point:
            if lower == self.low:
                return self.low
            if upper == self.high:
                return self.high
        return (lower + upper) / 2

    def reported_position(self, positions: Mapping[str, float]) -> int | None:
        """Group position to report for the given shade positions."""
        known = self._known(positions)
        if not known:
            return None
        hemline = self.common_hemline(positions)
        if hemline is None:
            hemline = sum(
                shade.hemline(positions[shade.entity_id]) for shade in known
            ) / len(known)
        return self.group_position_for(hemline)

    def shade(self, entity_id: str) -> Shade:
        """Look up a shade by entity id."""
        return next(shade for shade in self.shades if shade.entity_id == entity_id)

    def plan(
        self,
        positions: Mapping[str, float],
        target_hemline: float,
        pico_available: bool,
    ) -> Plan:
        """Plan the moves that bring every shade to `target_hemline`."""
        travel: dict[Direction, list[tuple[Shade, float, int]]] = {
            Direction.OPENING: [],
            Direction.CLOSING: [],
        }
        for shade in self._known(positions):
            current = positions[shade.entity_id]
            target = shade.position_for(target_hemline)
            if target == round(current):
                continue
            direction = Direction.OPENING if target > current else Direction.CLOSING
            travel[direction].append((shade, current, target))

        directions = [d for d, shades in travel.items() if shades]
        if not directions:
            return Plan(pico=None, moves=(), direction=None, duration=0.0)
        direction = directions[0] if len(directions) == 1 else None

        if (
            pico_available
            and direction is not None
            and self._pico_safe(positions, direction, len(travel[direction]))
        ):
            return self._pico_plan(direction, travel[direction])

        moves: list[Move] = []
        duration = 0.0
        for direction_, shades in travel.items():
            if not shades:
                continue
            # The shade furthest from the target leads; the rest start when the
            # leader's hemline reaches theirs.
            sign = 1 if direction_ is Direction.OPENING else -1
            leader, leader_position, _ = min(
                shades, key=lambda item: sign * item[0].hemline(item[1])
            )
            leader_hemline = leader.hemline(leader_position)
            for shade, current, target in shades:
                gap = sign * (shade.hemline(current) - leader_hemline)
                delay = gap / leader.speed
                moves.append(Move(shade.entity_id, target, delay))
                duration = max(
                    duration, delay + _travel_seconds(shade, current, target)
                )
        moves.sort(key=lambda move: move.delay)
        return Plan(
            pico=None,
            moves=tuple(moves),
            direction=direction,
            duration=duration,
            trips=tuple(moves),
        )

    def _pico_safe(
        self, positions: Mapping[str, float], direction: Direction, moving: int
    ) -> bool:
        """Whether a Pico press would start every shade from the same hemline.

        The Pico moves every paired shade that isn't already at the endpoint,
        so all of those must need to move and share a hemline (and all
        positions must be known).
        """
        if len(self._known(positions)) != len(self.shades):
            return False
        endpoint = 100 if direction is Direction.OPENING else 0
        hemlines = [
            shade.hemline(positions[shade.entity_id])
            for shade in self.shades
            if positions[shade.entity_id] != endpoint
        ]
        if len(hemlines) != moving:
            return False
        return max(hemlines) - min(hemlines) <= self.tolerance

    def _pico_plan(
        self, direction: Direction, shades: list[tuple[Shade, float, int]]
    ) -> Plan:
        endpoint = 100 if direction is Direction.OPENING else 0
        moves = tuple(
            Move(shade.entity_id, target, 0.0)
            for shade, _, target in shades
            if target != endpoint
        )
        duration = max(
            _travel_seconds(shade, current, target) for shade, current, target in shades
        )
        trips = tuple(Move(shade.entity_id, target, 0.0) for shade, _, target in shades)
        return Plan(
            pico=direction,
            moves=moves,
            direction=direction,
            duration=duration,
            trips=trips,
        )

    def _known(self, positions: Mapping[str, float]) -> list[Shade]:
        return [shade for shade in self.shades if shade.entity_id in positions]


def _travel_seconds(shade: Shade, current: float, target: float) -> float:
    return abs(target - current) / 100 * shade.travel_time
