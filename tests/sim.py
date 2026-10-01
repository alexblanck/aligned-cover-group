"""A simulated room of Lutron shades and a Pico, driven through real HA services.

Shades move over (frozen, manually advanced) time at their travel speed. The
Pico behaves like a Caseta shade Pico paired on the bridge: Up/Down send every
paired shade to open/closed at the same instant, and the middle button stops
moving shades but sends stationary shades to their favorite position.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.button import ButtonEntity
from homeassistant.components.cover import CoverEntity, CoverEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    setup_test_component_platform,
)

from custom_components.aligned_cover_group.const import DOMAIN

GROUP = "cover.living_room"
FAVORITE = 50
STEP_S = 0.25  # simulated time per tick


@dataclass
class ShadeSpec:
    """A physical shade: geometry as configured, and its true speed."""

    name: str
    closed_height: float
    open_height: float
    travel_time_s: float
    position_pct: float = 0

    @property
    def entity_id(self) -> str:
        return f"cover.{self.name}"


class SimShade(CoverEntity):
    """A shade that moves toward its target at a constant speed."""

    _attr_should_poll = False
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(self, spec: ShadeSpec, report_while_moving: bool) -> None:
        self.spec = spec
        self.entity_id = spec.entity_id
        self._attr_name = spec.name
        self._attr_unique_id = spec.name
        # The motor's true position: fractional, unlike what HA reports.
        self.position_pct = float(spec.position_pct)
        self.target_pct = self.position_pct
        self._report_while_moving = report_while_moving
        self._reported = round(self.position_pct)
        self._last = dt_util.utcnow()
        # (time, target_pct) log of motion starts, for synchronization checks.
        self.starts: list[tuple[float, float]] = []

    @property
    def moving(self) -> bool:
        return self.position_pct != self.target_pct

    @property
    def hemline_height(self) -> float:
        return self.spec.closed_height + self.position_pct / 100 * (
            self.spec.open_height - self.spec.closed_height
        )

    @property
    def current_cover_position(self) -> int:
        return self._reported

    @property
    def is_closed(self) -> bool:
        return self._reported == 0

    def settle(self) -> None:
        """Advance the motor to the current time."""
        now = dt_util.utcnow()
        elapsed_s = (now - self._last).total_seconds()
        self._last = now
        step_pct = elapsed_s * 100 / self.spec.travel_time_s
        if self.target_pct > self.position_pct:
            self.position_pct = min(self.target_pct, self.position_pct + step_pct)
        elif self.target_pct < self.position_pct:
            self.position_pct = max(self.target_pct, self.position_pct - step_pct)
        if self._report_while_moving or not self.moving:
            self._reported = round(self.position_pct)

    def go(self, target_pct: float) -> None:
        self.settle()
        if not self.moving and target_pct != self.position_pct:
            self.starts.append((dt_util.utcnow().timestamp(), target_pct))
        self.target_pct = target_pct
        self.async_write_ha_state()

    def stop(self) -> None:
        self.settle()
        self.target_pct = self.position_pct
        self._reported = round(self.position_pct)
        self.async_write_ha_state()

    def set_available(self, available: bool) -> None:
        """Drop off or come back, like a shade losing contact with the bridge."""
        self._attr_available = available
        self.async_write_ha_state()

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        self.go(kwargs["position"])

    async def async_open_cover(self, **kwargs: Any) -> None:
        self.go(100)

    async def async_close_cover(self, **kwargs: Any) -> None:
        self.go(0)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        self.stop()


class SimPicoButton(ButtonEntity):
    """One button of a shade Pico paired to `shades`."""

    _attr_should_poll = False

    def __init__(self, role: str, shades: list[SimShade]) -> None:
        self.role = role
        self.entity_id = f"button.pico_{role}"
        self._attr_name = f"Pico {role}"
        self._attr_unique_id = f"pico_{role}"
        self._shades = shades
        self.presses = 0

    async def async_press(self) -> None:
        self.presses += 1
        for shade in self._shades:
            shade.settle()
        if self.role == "open":
            targets = [100] * len(self._shades)
        elif self.role == "close":
            targets = [0] * len(self._shades)
        elif any(shade.moving for shade in self._shades):
            for shade in self._shades:
                shade.stop()
            return
        else:
            targets = [FAVORITE] * len(self._shades)
        for shade, target in zip(self._shades, targets, strict=True):
            shade.go(target)


class Room:
    """The simulated shades, Pico and aligned group under test."""

    def __init__(
        self,
        hass: HomeAssistant,
        freezer: FrozenDateTimeFactory,
        shades: list[SimShade],
        pico: dict[str, SimPicoButton] | None,
    ) -> None:
        self.hass = hass
        self.freezer = freezer
        self.shades = {shade.entity_id: shade for shade in shades}
        self.pico = pico
        self.entry: MockConfigEntry | None = None
        # Snapshot of every shade's hemline at every tick.
        self.history: list[dict[str, float]] = []

    def __getitem__(self, entity_id: str) -> SimShade:
        return self.shades[entity_id]

    async def add_group(self) -> None:
        """Create the aligned group through the config flow, like a user would."""
        flow = await self.hass.config_entries.flow.async_init(
            DOMAIN, context={"source": "user"}
        )
        group_input: dict[str, Any] = {
            "name": "Living Room",
            "covers": list(self.shades),
        }
        if self.pico:
            group_input |= {
                f"pico_{role}": button.entity_id for role, button in self.pico.items()
            }
        flow = await self.hass.config_entries.flow.async_configure(
            flow["flow_id"], group_input
        )
        for shade in self.shades.values():
            assert flow["step_id"] == "shade", flow
            flow = await self.hass.config_entries.flow.async_configure(
                flow["flow_id"],
                {
                    "open_height": shade.spec.open_height,
                    "closed_height": shade.spec.closed_height,
                    "travel_time_s": shade.spec.travel_time_s,
                },
            )
        assert flow["type"] == "create_entry", flow
        await self.hass.async_block_till_done()
        self.entry = self.hass.config_entries.async_entries(DOMAIN)[0]

    async def command(self, service: str, **data: Any) -> None:
        """Call a cover service on the group."""
        await self.hass.services.async_call(
            "cover", service, {"entity_id": GROUP, **data}, blocking=True
        )
        self._record()

    async def run(self, seconds: float) -> None:
        """Advance simulated time, letting timers fire and motors move."""
        elapsed_s = 0.0
        while elapsed_s < seconds:
            self.freezer.tick(timedelta(seconds=STEP_S))
            elapsed_s += STEP_S
            async_fire_time_changed(self.hass)
            await self.hass.async_block_till_done()
            for shade in self.shades.values():
                shade.settle()
                shade.async_write_ha_state()
            await self.hass.async_block_till_done()
            self._record()

    async def run_until_still(self, limit_s: float = 300) -> None:
        """Run until no shade is moving and the group isn't either."""
        elapsed_s = 0.0
        while elapsed_s < limit_s:
            await self.run(STEP_S)
            elapsed_s += STEP_S
            if not any(
                s.moving for s in self.shades.values()
            ) and self.group.state not in (
                "opening",
                "closing",
            ):
                return
        raise AssertionError("shades never stopped")

    @property
    def group(self):
        return self.hass.states.get(GROUP)

    def positions_pct_by_id(self) -> dict[str, int]:
        return {eid: round(shade.position_pct) for eid, shade in self.shades.items()}

    def _record(self) -> None:
        self.history.append(
            {eid: shade.position_pct for eid, shade in self.shades.items()}
        )

    def misalignment(self, positions_pct_by_id: dict[str, float]) -> float:
        """Smallest possible hemline spread, allowing shades to sit clamped.

        Independent of the integration's math: tries each shade's hemline as
        the shared height and measures the worst shade's distance from it.
        """
        specs = {eid: shade.spec for eid, shade in self.shades.items()}

        def hemline_height(eid: str) -> float:
            spec = specs[eid]
            return spec.closed_height + positions_pct_by_id[eid] / 100 * (
                spec.open_height - spec.closed_height
            )

        best = float("inf")
        for candidate in (hemline_height(eid) for eid in specs):
            worst = max(
                abs(
                    min(max(candidate, spec.closed_height), spec.open_height)
                    - hemline_height(eid)
                )
                for eid, spec in specs.items()
            )
            best = min(best, worst)
        return best

    def worst_misalignment(self) -> float:
        """Worst misalignment over the recorded history."""
        return max(self.misalignment(snapshot) for snapshot in self.history)


async def build_room(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    specs: list[ShadeSpec],
    pico: bool = True,
    report_while_moving: bool = True,
) -> Room:
    """Set up simulated shades (and Pico), then the group via its config flow."""
    shades = [SimShade(spec, report_while_moving) for spec in specs]
    setup_test_component_platform(hass, "cover", shades)
    assert await async_setup_component(hass, "cover", {"cover": {"platform": "test"}})
    buttons = None
    if pico:
        buttons = {
            role: SimPicoButton(role, shades) for role in ("open", "stop", "close")
        }
        setup_test_component_platform(hass, "button", list(buttons.values()))
        assert await async_setup_component(
            hass, "button", {"button": {"platform": "test"}}
        )
    await hass.async_block_till_done()
    room = Room(hass, freezer, shades, buttons)
    await room.add_group()
    return room
