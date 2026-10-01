"""The aligned cover group entity."""

from __future__ import annotations

import asyncio
import logging
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

from .alignment import Direction, Group, Plan, Shade, Trip
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
            travel_time_s=shade[CONF_TRAVEL_TIME_S],
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
        self._positions_pct: dict[str, int] = {}
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
        self._update_positions_pct()

    @callback
    def _async_member_changed(self, event: Event[EventStateChangedData]) -> None:
        self._update_positions_pct()
        self.async_write_ha_state()

    @callback
    def _update_positions_pct(self) -> None:
        self._positions_pct = {}
        for entity_id in self._entity_ids:
            state = self.hass.states.get(entity_id)
            if state is None:
                continue
            position_pct = state.attributes.get(ATTR_CURRENT_POSITION)
            if position_pct is not None:
                self._positions_pct[entity_id] = int(position_pct)

    @property
    def available(self) -> bool:
        """Available while any shade reports a position."""
        return bool(self._positions_pct)

    @property
    def current_cover_position(self) -> int | None:
        """Group position derived from the shades' hemlines."""
        return self._group.reported_position_pct(self._positions_pct)

    @property
    def is_closed(self) -> bool | None:
        """Closed when every shade is closed."""
        if not self._positions_pct:
            return None
        return all(pct <= 0 for pct in self._positions_pct.values())

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
            "aligned": (
                self._group.common_hemline_height(self._positions_pct) is not None
            ),
        }

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open every shade."""
        await self._async_move_to(self._group.open_height)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close every shade."""
        await self._async_move_to(self._group.closed_height)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the shared hemline to a group position."""
        await self._async_move_to(self._group.hemline_height_for(kwargs[ATTR_POSITION]))

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop every shade at once."""
        was_moving = self._moving
        self._cancel_motion()
        self.async_write_ha_state()
        # A shade Pico's middle button goes to the favorite position when the
        # shades are stationary, so only press it while we think they're moving.
        if self._pico and was_moving:
            _LOGGER.debug("%s: stopping with Pico %s", self.entity_id, self._pico[None])
            await self._async_call(BUTTON_DOMAIN, SERVICE_PRESS, self._pico[None])
        else:
            _LOGGER.debug(
                "%s: stopping each shade (%s)",
                self.entity_id,
                "no Pico"
                if not self._pico
                else "not moving, Pico would go to favorite",
            )
            await self._async_call(COVER_DOMAIN, SERVICE_STOP_COVER, self._entity_ids)

    async def _async_move_to(self, target_height: float) -> None:
        # Shades may not report position until they stop, so while our own
        # motion is running, plan from where we estimate the shades are.
        estimated = self._moving
        positions_pct = self._estimated_positions_pct()
        if missing := [e for e in self._entity_ids if e not in positions_pct]:
            _LOGGER.warning(
                "%s: leaving out shades with no position: %s",
                self.entity_id,
                ", ".join(missing),
            )
        self._cancel_motion()
        plan = self._group.plan(positions_pct, target_height, self._pico is not None)
        _LOGGER.debug(
            "%s: hemline height %.1f from %s positions %s -> "
            "Pico %s%s, trips %s, %.1fs",
            self.entity_id,
            target_height,
            "estimated" if estimated else "reported",
            positions_pct,
            plan.pico,
            f" ({plan.pico_blocker})" if plan.pico_blocker else "",
            [
                f"{t.shade.entity_id}->{t.target_pct}%@{t.delay_s:.1f}s"
                + ("" if t.needs_command else " (Pico)")
                for t in plan.trips
            ],
            plan.duration_s,
        )
        if not plan.trips:
            self.async_write_ha_state()
            return
        await self._async_run(plan)

    @callback
    def _estimated_positions_pct(self) -> dict[str, int]:
        if not self._moving:
            return dict(self._positions_pct)
        now = dt_util.utcnow()
        return {
            entity_id: (
                self._travel[entity_id].estimate_pct(now)
                if entity_id in self._travel
                else position_pct
            )
            for entity_id, position_pct in self._positions_pct.items()
        }

    async def _async_run(self, plan: Plan) -> None:
        generation = self._generation
        self._moving = True
        self._direction = plan.direction
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
            await self._async_call(BUTTON_DOMAIN, SERVICE_PRESS, self._pico[plan.pico])
        commands = [trip for trip in plan.trips if trip.needs_command]
        await self._async_set_positions(t for t in commands if t.delay_s <= 0)
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

    async def _async_delayed_start(self, trip: Trip, _now: datetime) -> None:
        _LOGGER.debug(
            "%s: starting %s -> %s%%",
            self.entity_id,
            trip.shade.entity_id,
            trip.target_pct,
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
    def _cancel_motion(self) -> None:
        self._generation += 1
        for cancel in self._timers:
            cancel()
        self._timers.clear()
        self._travel.clear()
        self._moving = False
        self._direction = None

    async def _async_set_positions(self, trips: Iterable[Trip]) -> None:
        await asyncio.gather(
            *(
                self.hass.services.async_call(
                    COVER_DOMAIN,
                    SERVICE_SET_COVER_POSITION,
                    {
                        ATTR_ENTITY_ID: trip.shade.entity_id,
                        ATTR_POSITION: trip.target_pct,
                    },
                    blocking=True,
                    context=self._context,
                )
                for trip in trips
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
