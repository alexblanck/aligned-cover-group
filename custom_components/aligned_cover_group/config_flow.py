"""Config and options flows for Aligned Cover Group."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.cover import CoverEntityFeature
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import (
    ATTR_FRIENDLY_NAME,
    ATTR_SUPPORTED_FEATURES,
    CONF_ENTITY_ID,
    CONF_NAME,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .const import (
    CONF_CLOSED_HEIGHT,
    CONF_COVERS,
    CONF_OPEN_HEIGHT,
    CONF_TRAVEL_TIME,
    DOMAIN,
    PICO_BUTTONS,
)

_HEIGHT = selector.NumberSelector(
    selector.NumberSelectorConfig(mode=selector.NumberSelectorMode.BOX, step="any")
)

SHADE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_OPEN_HEIGHT): _HEIGHT,
        vol.Required(CONF_CLOSED_HEIGHT): _HEIGHT,
        vol.Required(CONF_TRAVEL_TIME): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1,
                max=300,
                step=0.1,
                unit_of_measurement="s",
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
    }
)


def _group_schema(exclude: list[str]) -> vol.Schema:
    """Schema for choosing the covers and optional Pico buttons."""
    button = selector.EntitySelector(selector.EntitySelectorConfig(domain="button"))
    return vol.Schema(
        {
            vol.Required(CONF_COVERS): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="cover", multiple=True, exclude_entities=exclude
                )
            ),
            **{vol.Optional(key): button for key in PICO_BUTTONS},
        }
    )


def _validate_group(hass: HomeAssistant, user_input: dict[str, Any]) -> str | None:
    """Return an error key for invalid group input, or None."""
    if len(user_input[CONF_COVERS]) < 2:
        return "too_few_covers"
    for entity_id in user_input[CONF_COVERS]:
        state = hass.states.get(entity_id)
        if state is None or state.state == STATE_UNAVAILABLE:
            continue
        features = state.attributes.get(ATTR_SUPPORTED_FEATURES, 0)
        if not features & CoverEntityFeature.SET_POSITION:
            return "cover_no_position"
    pico = [user_input.get(key) for key in PICO_BUTTONS]
    if any(pico) and not all(pico):
        return "pico_incomplete"
    return None


class _ShadeSteps:
    """Steps shared by the config and options flows.

    After the group step, one "shade" step runs per selected cover to collect
    its heights and travel time.
    """

    hass: HomeAssistant
    _group: dict[str, Any]
    _shades: list[dict[str, Any]]
    _previous: dict[str, dict[str, Any]]

    def _start_shades(self, group: dict[str, Any]) -> None:
        self._group = {key: value for key, value in group.items() if value}
        self._shades = []

    async def async_step_shade(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect heights and travel time for one shade."""
        entity_ids: list[str] = self._group[CONF_COVERS]
        entity_id = entity_ids[len(self._shades)]
        errors: dict[str, str] = {}

        if user_input is not None:
            if user_input[CONF_CLOSED_HEIGHT] >= user_input[CONF_OPEN_HEIGHT]:
                errors["base"] = "closed_not_below_open"
            else:
                self._shades.append({CONF_ENTITY_ID: entity_id, **user_input})
                if len(self._shades) == len(entity_ids):
                    return self._async_finish(
                        {**self._group, CONF_COVERS: self._shades}
                    )
                return await self.async_step_shade()

        state = self.hass.states.get(entity_id)
        name = (
            state.attributes.get(ATTR_FRIENDLY_NAME, entity_id) if state else entity_id
        )
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="shade",
            data_schema=self.add_suggested_values_to_schema(  # type: ignore[attr-defined]
                SHADE_SCHEMA, user_input or self._previous.get(entity_id, {})
            ),
            errors=errors,
            description_placeholders={
                "name": name,
                "index": str(len(self._shades) + 1),
                "count": str(len(entity_ids)),
            },
        )

    @callback
    def _async_finish(self, options: dict[str, Any]) -> ConfigFlowResult:
        raise NotImplementedError


class AlignedCoverGroupConfigFlow(_ShadeSteps, ConfigFlow, domain=DOMAIN):
    """Create an aligned cover group."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._previous = {}

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> AlignedCoverGroupOptionsFlow:
        """Return the options flow."""
        return AlignedCoverGroupOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose a name, the covers and an optional Pico."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if error := _validate_group(self.hass, user_input):
                errors["base"] = error
            else:
                self._name = user_input.pop(CONF_NAME)
                self._start_shades(user_input)
                return await self.async_step_shade()

        schema = vol.Schema({vol.Required(CONF_NAME): selector.TextSelector()}).extend(
            _group_schema([]).schema
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
        )

    @callback
    def _async_finish(self, options: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(title=self._name, data={}, options=options)


class AlignedCoverGroupOptionsFlow(_ShadeSteps, OptionsFlowWithReload):
    """Edit an aligned cover group."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the covers and Pico."""
        options = self.config_entry.options
        self._previous = {
            shade[CONF_ENTITY_ID]: shade for shade in options.get(CONF_COVERS, [])
        }
        errors: dict[str, str] = {}
        if user_input is not None:
            if error := _validate_group(self.hass, user_input):
                errors["base"] = error
            else:
                self._start_shades(user_input)
                return await self.async_step_shade()

        own_entities = [
            entry.entity_id
            for entry in er.async_entries_for_config_entry(
                er.async_get(self.hass), self.config_entry.entry_id
            )
        ]
        suggested = user_input or {**options, CONF_COVERS: list(self._previous)}
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                _group_schema(own_entities), suggested
            ),
            errors=errors,
        )

    @callback
    def _async_finish(self, options: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(data=options)
