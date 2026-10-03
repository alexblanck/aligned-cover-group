"""Roll profiles: how a roller shade's hemline height follows its position.

Pure math with no Home Assistant imports, like `alignment`, which builds shades
and groups on top of these. See `alignment` for the naming conventions.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


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
class RollProfile:
    """How hemline height follows motor rotation for one kind of roll.

    Positions count motor rotation, as on most motorized roller shades,
    including the Lutron Serena shades this was built and tested with. Their
    motors turn at a steady speed, so a position is also a share of the run
    time. Rotation isn't proportional to height, though: the roll is fattest
    when open, so the hemline moves faster near the top. A roll that shrinks
    steadily as it unwinds makes height a quadratic in position, so the
    profile is the quadratic through the measured shade's heights at closed
    (0%), halfway (50%) and open (100%). With the halfway height at the
    midpoint it's a straight line.

    Shades whose rolls match at every hemline height all share this profile,
    each seeing its own part of it (see `view`), so profile positions may lie
    beyond 0-100% for shades reaching past the measured one.
    """

    closed_height: float
    open_height: float
    halfway_height: float
    # Coefficients of height = closed + span * (rise * x + bend * x**2), where
    # x is the position as a fraction.
    rise: float = field(init=False, repr=False)
    bend: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Reject curves no shade could follow (see `halfway_height_range`)."""
        if not self.open_height > self.closed_height:
            raise ValueError(
                f"Open height {self.open_height} must be above "
                f"closed height {self.closed_height}"
            )
        low, high = halfway_height_range(self.closed_height, self.open_height)
        if not low < self.halfway_height <= high:
            raise ValueError(
                f"Halfway height {self.halfway_height} must be above {low} "
                f"and at most {high}"
            )
        halfway_fraction = (self.halfway_height - self.closed_height) / self._span
        object.__setattr__(self, "rise", 4 * halfway_fraction - 1)
        object.__setattr__(self, "bend", 2 - 4 * halfway_fraction)

    @classmethod
    def straight(cls, closed_height: float, open_height: float) -> RollProfile:
        """A profile where height is proportional to position."""
        return cls(
            closed_height,
            open_height,
            closed_height + (open_height - closed_height) / 2,
        )

    def height_at(self, position_pct: float) -> float:
        """Hemline height at a position (which may be beyond 0-100%)."""
        x = position_pct / 100
        return self.closed_height + self._span * (self.rise * x + self.bend * x**2)

    def position_pct_at(self, hemline_height: float) -> float:
        """Unrounded position in percent for a height, possibly beyond 0-100%.

        Raises ValueError for heights the profile never reaches: below its
        range, it eventually flattens out, as if the roll ran out of fabric.
        """
        fraction_up = (hemline_height - self.closed_height) / self._span
        # Solves rise * x + bend * x**2 = fraction_up for x, on the rising side
        # of the curve. With d = rise**2 + 4 * bend * fraction_up, the usual
        # quadratic formula is
        #     x = (-rise + sqrt(d)) / (2 * bend)
        # which divides by bend, zero for a straight line. Multiplying top and
        # bottom by (rise + sqrt(d)) gives this equivalent form, which doesn't,
        # and becomes fraction_up / rise when bend is zero.
        discriminant = self.rise**2 + 4 * self.bend * fraction_up
        denominator = self.rise + math.sqrt(max(discriminant, 0.0))
        if discriminant < 0 or denominator <= 0:
            raise ValueError(f"The roll profile never reaches {hemline_height}")
        return 100 * 2 * fraction_up / denominator

    def view(self, closed_height: float, open_height: float) -> RollProfileView:
        """The part of this profile between two heights, as its own 0-100%."""
        return RollProfileView(self, closed_height, open_height)

    @property
    def _span(self) -> float:
        return self.open_height - self.closed_height


@dataclass(frozen=True)
class RollProfileView:
    """Part of a roll profile, between two heights, as its own 0-100%.

    A shade sees the part between its limits (and the group the part between
    the lowest and highest of them). The view runs from `closed_height` (0%)
    to `open_height` (100%), which fall at profile positions `closed_pct` and
    `open_pct`, and delegates to the profile: within the view, positions
    rescale in a straight line (the motor turns at a steady speed, so each
    percent is the same amount of rotation), and heights and positions outside
    it are clamped to its ends. A shade with its own profile sees all of it.
    Raises ValueError if the profile never reaches one of the heights.
    """

    profile: RollProfile
    closed_height: float
    open_height: float
    closed_pct: float = field(init=False)
    open_pct: float = field(init=False)

    def __post_init__(self) -> None:
        """Find where the view's ends fall on the profile."""
        object.__setattr__(
            self, "closed_pct", self.profile.position_pct_at(self.closed_height)
        )
        object.__setattr__(
            self, "open_pct", self.profile.position_pct_at(self.open_height)
        )

    @property
    def size_pct(self) -> float:
        """How much of the profile's rotation the view covers, in profile percent."""
        return self.open_pct - self.closed_pct

    def height_at(self, position_pct: float) -> float:
        """Hemline height at a position in this view (exact at its ends)."""
        if position_pct <= 0:
            return self.closed_height
        if position_pct >= 100:
            return self.open_height
        return self.profile.height_at(
            self.closed_pct + position_pct / 100 * self.size_pct
        )

    def clamp(self, hemline_height: float) -> float:
        """The closest height within the view."""
        return min(max(hemline_height, self.closed_height), self.open_height)

    def position_pct_at(self, hemline_height: float) -> float:
        """Unrounded position in this view for a height."""
        profile_pct = self.profile.position_pct_at(self.clamp(hemline_height))
        return (profile_pct - self.closed_pct) / self.size_pct * 100
