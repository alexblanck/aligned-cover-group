"""The aligned cover group entity."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from homeassistant.components.button.const import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button.const import SERVICE_PRESS
from homeassistant.components.cover import (
    ATTR_CURRENT_POSITION,
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.components.cover import (
    DOMAIN as COVER_DOMAIN,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ENTITY_ID,
    SERVICE_SET_COVER_POSITION,
    SERVICE_STOP_COVER,
)
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .alignment import AlignmentGroup, Direction, Plan, Shade, Trip
from .const import (
    CONF_CLOSED_HEIGHT,
    CONF_COVERS,
    CONF_OPEN_HEIGHT,
    CONF_PICO_CLOSE,
    CONF_PICO_OPEN,
    CONF_PICO_STOP,
    CONF_TRAVEL_TIME_S,
)

_LOGGER = logging.getLogger(__name__)

ATTR_HEMLINE_HEIGHTS = "hemline_heights"

# Extra time after the planned motion before the group reports it has stopped.
MOTION_END_MARGIN_S = 2.0


@dataclass(frozen=True)
class _Travel:
    """A shade's planned trip, used to estimate where it is mid-motion."""

    start: datetime
    from_pct: int
    to_pct: int
    travel_time_s: float

    def estimate_pct(self, now: datetime) -> int:
        elapsed_s = max(0.0, (now - self.start).total_seconds())
        moved_pct = elapsed_s / self.travel_time_s * 100
        if self.to_pct > self.from_pct:
            return round(min(self.to_pct, self.from_pct + moved_pct))
        return round(max(self.to_pct, self.from_pct - moved_pct))


@dataclass(frozen=True)
class PicoButtons:
    """Button entities of a Pico paired to exactly the group's shades."""

    open: str
    stop: str
    close: str

    def toward(self, direction: Direction) -> str:
        """The button that sends every shade toward that direction's end."""
        return self.open if direction is Direction.OPENING else self.close


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the aligned cover group entity."""
    options = entry.options
    group = AlignmentGroup(
        Shade(
            entity_id=shade[CONF_ENTITY_ID],
            closed_height=shade[CONF_CLOSED_HEIGHT],
            open_height=shade[CONF_OPEN_HEIGHT],
            travel_time_s=shade[CONF_TRAVEL_TIME_S],
        )
        for shade in options[CONF_COVERS]
    )
    pico = None
    if options.get(CONF_PICO_OPEN):
        pico = PicoButtons(
            open=options[CONF_PICO_OPEN],
            stop=options[CONF_PICO_STOP],
            close=options[CONF_PICO_CLOSE],
        )
    async_add_entities([AlignedCoverGroup(entry, group, pico)])


class AlignedCoverGroup(CoverEntity):
    """A cover group that keeps its shades' hemlines aligned."""

    _attr_should_poll = False
    _attr_device_class = CoverDeviceClass.SHADE
    # Derived from the shades' own recorded states; no need to store them too.
    _unrecorded_attributes = frozenset({ATTR_ENTITY_ID, ATTR_HEMLINE_HEIGHTS})
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(
        self,
        entry: ConfigEntry,
        group: AlignmentGroup,
        pico: PicoButtons | None,
    ) -> None:
        """Initialize the group."""
        self._attr_name = entry.title
        self._attr_unique_id = entry.entry_id
        self._group = group
        self._pico = pico
        self._entity_ids = [shade.entity_id for shade in group.shades]
        self._positions_pct_by_id: dict[str, int] = {}
        # Set while a planned motion is (believed to be) in progress.
        self._moving = False
        self._direction: Direction | None = None
        self._timers: list[CALLBACK_TYPE] = []
        # Planned trips of the current motion, keyed by entity id.
        self._travel: dict[str, _Travel] = {}
        # Bumped on every command so stale scheduled moves can tell they're stale.
        self._generation = 0

    async def async_added_to_hass(self) -> None:
        """Track the member shades."""
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, self._entity_ids, self._async_member_changed
            )
        )
        self.async_on_remove(self._abandon_plan)
        self._update_positions_pct_by_id()

    @callback
    def _async_member_changed(self, event: Event[EventStateChangedData]) -> None:
        self._update_positions_pct_by_id()
        self.async_write_ha_state()

    @callback
    def _update_positions_pct_by_id(self) -> None:
        self._positions_pct_by_id = {
            entity_id: position_pct
            for entity_id in self._entity_ids
            if (position_pct := self._current_shade_position_pct(entity_id)) is not None
        }

    def _current_shade_position_pct(self, entity_id: str) -> int | None:
        if (state := self.hass.states.get(entity_id)) is None:
            return None
        position_pct = state.attributes.get(ATTR_CURRENT_POSITION)
        return None if position_pct is None else int(position_pct)

    @property
    def available(self) -> bool:
        """Available while any shade reports a position."""
        return bool(self._positions_pct_by_id)

    @property
    def current_cover_position(self) -> int | None:
        """Group position derived from the shades' hemlines."""
        return self._group.current_group_position_pct(self._positions_pct_by_id)

    @property
    def is_closed(self) -> bool | None:
        """Closed when every shade is closed."""
        if not self._positions_pct_by_id:
            return None
        return all(pct <= 0 for pct in self._positions_pct_by_id.values())

    @property
    def is_opening(self) -> bool:
        """Whether a planned motion is opening the group."""
        return self._moving and self._direction is Direction.OPENING

    @property
    def is_closing(self) -> bool:
        """Whether a planned motion is closing the group."""
        return self._moving and self._direction is Direction.CLOSING

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose members and alignment.

        Members go in `entity_id` rather than HA's `group_entities`: that comes
        from setting `self.group`, which makes HA send service calls straight
        to the members, bypassing alignment and the Pico.
        """
        return {
            ATTR_ENTITY_ID: self._entity_ids,
            "aligned": (
                self._group.common_hemline_height(self._positions_pct_by_id) is not None
            ),
            ATTR_HEMLINE_HEIGHTS: {
                shade.entity_id: round(
                    shade.hemline_height(self._positions_pct_by_id[shade.entity_id]), 1
                )
                for shade in self._group.shades
                if shade.entity_id in self._positions_pct_by_id
            },
        }

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open every shade."""
        await self._async_move_to(100)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close every shade."""
        await self._async_move_to(0)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the shared hemline to a group position."""
        await self._async_move_to(kwargs[ATTR_POSITION])

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop every shade at once."""
        was_moving = self._moving
        self._abandon_plan()
        self.async_write_ha_state()
        # A shade Pico's middle button goes to the favorite position when the
        # shades are stationary, so only press it while we think they're moving.
        if self._pico and was_moving:
            await self._async_press(self._pico.stop)
        else:
            took_s = await self._async_call(
                COVER_DOMAIN, SERVICE_STOP_COVER, self._entity_ids
            )
            _LOGGER.debug(
                "%s: stopped each shade (%s) in %.3fs",
                self.entity_id,
                "no Pico"
                if not self._pico
                else "not moving, Pico would go to favorite",
                took_s,
            )

    async def _async_move_to(self, target_pct: int) -> None:
        positions_pct_by_id, positions_source = self._planning_positions_pct_by_id()
        if missing := [e for e in self._entity_ids if e not in positions_pct_by_id]:
            _LOGGER.warning(
                "%s: leaving out shades with no position: %s",
                self.entity_id,
                ", ".join(missing),
            )
        self._abandon_plan()
        plan = self._group.plan(positions_pct_by_id, target_pct, self._pico is not None)
        direction = self._group_direction(positions_pct_by_id, target_pct)
        _LOGGER.debug(
            "%s: to %s%% (hemline height %.1f) from %s positions %s -> "
            "Pico %s%s, trips %s, %.1fs",
            self.entity_id,
            target_pct,
            self._group.hemline_height_for(target_pct),
            positions_source,
            positions_pct_by_id,
            plan.pico,
            f" ({plan.pico_blocker})" if plan.pico_blocker else "",
            [
                f"{t.shade.entity_id}->{t.target_pct}%@{t.delay_s:.1f}s"
                + ("" if t.needs_command else " (Pico)")
                for t in plan.trips
            ],
            plan.duration_s,
        )
        if plan.trips:
            await self._async_run(plan, direction)
        else:
            self.async_write_ha_state()  # a previous move may have just been abandoned

    @callback
    def _planning_positions_pct_by_id(self) -> tuple[dict[str, int], str]:
        """Shade positions to plan a move from, and their source.

        Shades may not report position until they stop, so while our own move
        is running these are estimates; otherwise they're what shades reported.
        """
        if not self._moving:
            return dict(self._positions_pct_by_id), "reported"
        now = dt_util.utcnow()
        return {
            entity_id: (
                self._travel[entity_id].estimate_pct(now)
                if entity_id in self._travel
                else position_pct
            )
            for entity_id, position_pct in self._positions_pct_by_id.items()
        }, "estimated"

    def _group_direction(
        self, positions_pct_by_id: dict[str, int], target_pct: int
    ) -> Direction | None:
        """Which way the group's own position moves, whatever each shade does."""
        current_pct = self._group.current_group_position_pct(positions_pct_by_id)
        if current_pct is None or target_pct == current_pct:
            return None
        return Direction.OPENING if target_pct > current_pct else Direction.CLOSING

    async def _async_run(self, plan: Plan, direction: Direction | None) -> None:
        generation = self._generation
        self._moving = True
        self._direction = direction
        now = dt_util.utcnow()
        self._travel = {
            trip.shade.entity_id: _Travel(
                start=now + timedelta(seconds=trip.delay_s),
                from_pct=trip.from_pct,
                to_pct=trip.target_pct,
                travel_time_s=trip.shade.travel_time_s,
            )
            for trip in plan.trips
        }
        self.async_write_ha_state()

        if plan.pico is not None and self._pico:
            await self._async_press(self._pico.toward(plan.pico))
        commands = [trip for trip in plan.trips if trip.needs_command]
        await self._async_set_positions(t for t in commands if t.delay_s <= 0)
        # A stop or new move may have arrived while we awaited the commands.
        if generation != self._generation:
            _LOGGER.debug(
                "%s: superseded while starting; not scheduling delayed starts",
                self.entity_id,
            )
            return

        for trip in commands:
            if trip.delay_s > 0:
                self._timers.append(
                    async_call_later(
                        self.hass,
                        trip.delay_s,
                        partial(self._async_delayed_start, trip),
                    )
                )
        self._timers.append(
            async_call_later(
                self.hass,
                plan.duration_s + MOTION_END_MARGIN_S,
                self._async_motion_done,
            )
        )

    async def _async_delayed_start(self, trip: Trip, now: datetime) -> None:
        scheduled = self._travel[trip.shade.entity_id].start
        _LOGGER.debug(
            "%s: starting %s -> %s%% (timer %+.3fs from schedule)",
            self.entity_id,
            trip.shade.entity_id,
            trip.target_pct,
            (now - scheduled).total_seconds(),
        )
        await self._async_set_positions([trip])

    @callback
    def _async_motion_done(self, _now: datetime) -> None:
        _LOGGER.debug("%s: motion finished", self.entity_id)
        self._timers.clear()
        self._travel.clear()
        self._moving = False
        self._direction = None
        self.async_write_ha_state()

    @callback
    def _abandon_plan(self) -> None:
        """Stop following the current plan.

        Cancels starts that haven't happened yet and forgets the position
        estimates. Shades that are already moving keep moving.
        """
        self._generation += 1
        for cancel in self._timers:
            cancel()
        self._timers.clear()
        self._travel.clear()
        self._moving = False
        self._direction = None

    async def _async_set_positions(self, trips: Iterable[Trip]) -> None:
        """Send the trips' commands at once and log how long each took."""
        trips = list(trips)
        if not trips:
            return
        took_s = await asyncio.gather(
            *(
                self._async_call(
                    COVER_DOMAIN,
                    SERVICE_SET_COVER_POSITION,
                    trip.shade.entity_id,
                    {ATTR_POSITION: trip.target_pct},
                )
                for trip in trips
            )
        )
        _LOGGER.debug(
            "%s: set positions in %.3fs: %s",
            self.entity_id,
            max(took_s),
            {
                trip.shade.entity_id: f"{trip.target_pct}% in {seconds:.3f}s"
                for trip, seconds in zip(trips, took_s, strict=True)
            },
        )

    async def _async_press(self, button_entity_id: str) -> None:
        took_s = await self._async_call(BUTTON_DOMAIN, SERVICE_PRESS, button_entity_id)
        _LOGGER.debug(
            "%s: pressed Pico %s in %.3fs", self.entity_id, button_entity_id, took_s
        )

    async def _async_call(
        self,
        domain: str,
        service: str,
        entity_id: str | list[str],
        data: dict[str, Any] | None = None,
    ) -> float:
        """Call a service and return how long it took, in seconds."""
        started = time.monotonic()
        await self.hass.services.async_call(
            domain,
            service,
            {ATTR_ENTITY_ID: entity_id, **(data or {})},
            blocking=True,
            context=self._context,
        )
        return time.monotonic() - started
