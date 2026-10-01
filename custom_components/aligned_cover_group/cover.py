"""The aligned cover group entity."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import SERVICE_PRESS
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
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .alignment import Direction, Group, Move, Plan, Shade
from .const import (
    CONF_CLOSED_HEIGHT,
    CONF_COVERS,
    CONF_OPEN_HEIGHT,
    CONF_PICO_CLOSE,
    CONF_PICO_OPEN,
    CONF_PICO_STOP,
    CONF_TRAVEL_TIME,
)

# Extra time after the planned motion before the group reports it has stopped.
MOTION_END_MARGIN = 2.0


@dataclass(frozen=True)
class _Travel:
    """A shade's planned trip, used to estimate where it is mid-motion."""

    start: datetime
    position: float
    target: int
    travel_time: float

    def estimate(self, now: datetime) -> float:
        elapsed = max(0.0, (now - self.start).total_seconds())
        moved = elapsed / self.travel_time * 100
        if self.target > self.position:
            return min(self.target, self.position + moved)
        return max(self.target, self.position - moved)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the aligned cover group entity."""
    options = entry.options
    group = Group(
        Shade(
            entity_id=shade[CONF_ENTITY_ID],
            closed_height=shade[CONF_CLOSED_HEIGHT],
            open_height=shade[CONF_OPEN_HEIGHT],
            travel_time=shade[CONF_TRAVEL_TIME],
        )
        for shade in options[CONF_COVERS]
    )
    pico = None
    if options.get(CONF_PICO_OPEN):
        pico = {
            Direction.OPENING: options[CONF_PICO_OPEN],
            Direction.CLOSING: options[CONF_PICO_CLOSE],
            None: options[CONF_PICO_STOP],
        }
    async_add_entities([AlignedCoverGroup(entry, group, pico)])


class AlignedCoverGroup(CoverEntity):
    """A cover group that keeps its shades' hemlines aligned."""

    _attr_should_poll = False
    _attr_device_class = CoverDeviceClass.SHADE
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(
        self,
        entry: ConfigEntry,
        group: Group,
        pico: dict[Direction | None, str] | None,
    ) -> None:
        """Initialize the group."""
        self._attr_name = entry.title
        self._attr_unique_id = entry.entry_id
        self._group = group
        self._pico = pico
        self._entity_ids = [shade.entity_id for shade in group.shades]
        self._positions: dict[str, float] = {}
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
        self.async_on_remove(self._cancel_motion)
        self._update_positions()

    @callback
    def _async_member_changed(self, event: Event) -> None:
        self._update_positions()
        self.async_write_ha_state()

    @callback
    def _update_positions(self) -> None:
        self._positions = {}
        for entity_id in self._entity_ids:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            position = state.attributes.get(ATTR_CURRENT_POSITION)
            if position is not None:
                self._positions[entity_id] = float(position)

    @property
    def available(self) -> bool:
        """Available while any shade reports a position."""
        return bool(self._positions)

    @property
    def current_cover_position(self) -> int | None:
        """Group position derived from the shades' hemlines."""
        return self._group.reported_position(self._positions)

    @property
    def is_closed(self) -> bool | None:
        """Closed when every shade is closed."""
        if not self._positions:
            return None
        return all(position <= 0 for position in self._positions.values())

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
        """Expose members and alignment."""
        return {
            ATTR_ENTITY_ID: self._entity_ids,
            "aligned": self._group.common_hemline(self._positions) is not None,
        }

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open every shade."""
        await self._async_move_to(self._group.high)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close every shade."""
        await self._async_move_to(self._group.low)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the shared hemline to a group position."""
        await self._async_move_to(self._group.hemline_for(kwargs[ATTR_POSITION]))

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop every shade at once."""
        was_moving = self._moving
        self._cancel_motion()
        self.async_write_ha_state()
        # A shade Pico's middle button goes to the favorite position when the
        # shades are stationary, so only press it while we think they're moving.
        if self._pico and was_moving:
            await self._async_call(BUTTON_DOMAIN, SERVICE_PRESS, self._pico[None])
        else:
            await self._async_call(COVER_DOMAIN, SERVICE_STOP_COVER, self._entity_ids)

    async def _async_move_to(self, hemline: float) -> None:
        # Shades may not report position until they stop, so while our own
        # motion is running, plan from where we estimate the shades are.
        positions = self._estimated_positions()
        self._cancel_motion()
        plan = self._group.plan(positions, hemline, self._pico is not None)
        if not plan.moves and plan.pico is None:
            self.async_write_ha_state()
            return
        await self._async_run(plan, positions)

    @callback
    def _estimated_positions(self) -> dict[str, float]:
        if not self._moving:
            return dict(self._positions)
        now = dt_util.utcnow()
        return {
            entity_id: (
                self._travel[entity_id].estimate(now)
                if entity_id in self._travel
                else position
            )
            for entity_id, position in self._positions.items()
        }

    async def _async_run(self, plan: Plan, positions: dict[str, float]) -> None:
        generation = self._generation
        self._moving = True
        self._direction = plan.direction
        now = dt_util.utcnow()
        self._travel = {
            trip.entity_id: _Travel(
                start=now + timedelta(seconds=trip.delay),
                position=positions[trip.entity_id],
                target=trip.position,
                travel_time=self._group.shade(trip.entity_id).travel_time,
            )
            for trip in plan.trips
        }
        self.async_write_ha_state()

        if plan.pico is not None and self._pico:
            await self._async_call(BUTTON_DOMAIN, SERVICE_PRESS, self._pico[plan.pico])
        now = [move for move in plan.moves if move.delay <= 0]
        await self._async_set_positions(now)
        if generation != self._generation:
            # Stopped or retargeted while the first commands were in flight.
            return

        for move in plan.moves:
            if move.delay > 0:
                self._timers.append(
                    async_call_later(
                        self.hass, move.delay, partial(self._async_delayed_move, move)
                    )
                )
        self._timers.append(
            async_call_later(
                self.hass, plan.duration + MOTION_END_MARGIN, self._async_motion_done
            )
        )

    async def _async_delayed_move(self, move: Move, _now: datetime) -> None:
        await self._async_set_positions([move])

    @callback
    def _async_motion_done(self, _now: datetime) -> None:
        self._timers.clear()
        self._travel.clear()
        self._moving = False
        self._direction = None
        self.async_write_ha_state()

    @callback
    def _cancel_motion(self) -> None:
        self._generation += 1
        for cancel in self._timers:
            cancel()
        self._timers.clear()
        self._travel.clear()
        self._moving = False
        self._direction = None

    async def _async_set_positions(self, moves: Iterable[Move]) -> None:
        await asyncio.gather(
            *(
                self.hass.services.async_call(
                    COVER_DOMAIN,
                    SERVICE_SET_COVER_POSITION,
                    {ATTR_ENTITY_ID: move.entity_id, ATTR_POSITION: move.position},
                    blocking=True,
                    context=self._context,
                )
                for move in moves
            )
        )

    async def _async_call(
        self, domain: str, service: str, entity_id: str | list[str]
    ) -> None:
        await self.hass.services.async_call(
            domain,
            service,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
            context=self._context,
        )
