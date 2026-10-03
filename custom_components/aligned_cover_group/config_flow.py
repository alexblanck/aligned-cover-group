"""Config and options flows for Aligned Cover Group."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.cover import CoverEntityFeature
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryBaseFlow,
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
from homeassistant.data_entry_flow import section
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .alignment import halfway_height_range
from .const import (
    CONF_CLOSED_HEIGHT,
    CONF_COVERS,
    CONF_HALFWAY_HEIGHT,
    CONF_OPEN_HEIGHT,
    CONF_TRAVEL_TIME_S,
    DOMAIN,
    PICO_BUTTONS,
    PICO_SECTION,
)

_HEIGHT = selector.NumberSelector(
    selector.NumberSelectorConfig(mode=selector.NumberSelectorMode.BOX, step="any")
)

SHADE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_CLOSED_HEIGHT): _HEIGHT,
        vol.Required(CONF_OPEN_HEIGHT): _HEIGHT,
    }
)

TRAVEL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TRAVEL_TIME_S): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1,
                max=300,
                step=0.1,
                unit_of_measurement="s",
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
        vol.Optional(CONF_HALFWAY_HEIGHT): _HEIGHT,
    }
)


def _group_schema(exclude: list[str], pico_collapsed: bool) -> vol.Schema:
    """Schema for choosing the covers and, in a section, optional Pico buttons."""
    button = selector.EntitySelector(
        selector.EntitySelectorConfig(domain="button", integration="lutron_caseta")
    )
    return vol.Schema(
        {
            vol.Required(CONF_COVERS): selector.EntitySelector(
                selector.EntitySelectorConfig(
                    domain="cover", multiple=True, exclude_entities=exclude
                )
            ),
            vol.Required(PICO_SECTION): section(
                vol.Schema({vol.Optional(key): button for key in PICO_BUTTONS}),
                {"collapsed": pico_collapsed},
            ),
        }
    )


def _flatten_pico(user_input: dict[str, Any]) -> dict[str, Any]:
    """Group input with the Pico section's buttons moved to the top level."""
    pico: dict[str, Any] = user_input.get(PICO_SECTION, {})
    flat = {key: value for key, value in user_input.items() if key != PICO_SECTION}
    return flat | pico


REQUIRED_FEATURES = CoverEntityFeature.SET_POSITION | CoverEntityFeature.STOP


def _validate_group(hass: HomeAssistant, user_input: dict[str, Any]) -> str | None:
    """Return an error key for invalid group input, or None."""
    if len(user_input[CONF_COVERS]) < 2:
        return "too_few_covers"
    for entity_id in user_input[CONF_COVERS]:
        features = _supported_features(hass, entity_id)
        if features is not None and features & REQUIRED_FEATURES != REQUIRED_FEATURES:
            return "cover_unsupported"
    pico = [user_input.get(key) for key in PICO_BUTTONS]
    if any(pico) and not all(pico):
        return "pico_incomplete"
    if all(pico) and len(set(pico)) != len(pico):
        return "pico_duplicate"
    return None


def _supported_features(hass: HomeAssistant, entity_id: str) -> int | None:
    """A cover's features, from its state or, if unavailable, the registry."""
    state = hass.states.get(entity_id)
    if state is not None and state.state != STATE_UNAVAILABLE:
        return int(state.attributes.get(ATTR_SUPPORTED_FEATURES, 0))
    if (entry := er.async_get(hass).async_get(entity_id)) is not None:
        return entry.supported_features
    return None


def _ranges_overlap(shades: list[dict[str, Any]]) -> bool:
    """Whether some hemline height is within every shade's range."""
    highest_closed = max(shade[CONF_CLOSED_HEIGHT] for shade in shades)
    lowest_open = min(shade[CONF_OPEN_HEIGHT] for shade in shades)
    return bool(highest_closed < lowest_open)


class _ShadeSteps(ConfigEntryBaseFlow):
    """Steps shared by the config and options flows.

    After the group step, one "shade" step runs per selected cover to collect
    its heights, then a "travel" step asks for the tallest shade's travel time
    (all shades are assumed to move at the same speed).
    """

    _group: dict[str, Any]
    _shades: list[dict[str, Any]]
    _previous: dict[str, dict[str, Any]]
    _previous_travel: dict[str, Any]

    def _start_shades(self, group: dict[str, Any]) -> None:
        self._group = {key: value for key, value in group.items() if value}
        self._shades = []

    async def async_step_shade(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect heights for one shade."""
        entity_ids: list[str] = self._group[CONF_COVERS]
        entity_id = entity_ids[len(self._shades)]
        errors: dict[str, str] = {}

        if user_input is not None:
            if user_input[CONF_CLOSED_HEIGHT] >= user_input[CONF_OPEN_HEIGHT]:
                errors["base"] = "closed_not_below_open"
            elif not _ranges_overlap([*self._shades, user_input]):
                errors["base"] = "ranges_do_not_overlap"
            else:
                self._shades.append({CONF_ENTITY_ID: entity_id, **user_input})
                if len(self._shades) == len(entity_ids):
                    return await self.async_step_travel()
                return await self.async_step_shade()

        return self.async_show_form(
            step_id="shade",
            data_schema=self.add_suggested_values_to_schema(
                SHADE_SCHEMA, user_input or self._previous.get(entity_id, {})
            ),
            errors=errors,
            description_placeholders={
                "name": self._friendly_name(entity_id),
                "index": str(len(self._shades) + 1),
                "count": str(len(entity_ids)),
            },
        )

    async def async_step_travel(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the tallest shade's travel time and, optionally, its curve."""
        tallest = max(
            self._shades,
            key=lambda shade: shade[CONF_OPEN_HEIGHT] - shade[CONF_CLOSED_HEIGHT],
        )
        low, high = halfway_height_range(
            tallest[CONF_CLOSED_HEIGHT], tallest[CONF_OPEN_HEIGHT]
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            halfway_height = user_input.get(CONF_HALFWAY_HEIGHT)
            if halfway_height is None:
                pass
            elif not low < halfway_height <= high:
                errors["base"] = "halfway_out_of_range"
            elif not self._tallest_spans_group(tallest):
                errors["base"] = "halfway_needs_shared_roll"
            if not errors:
                return self._async_finish(
                    {**self._group, CONF_COVERS: self._shades, **user_input}
                )

        return self.async_show_form(
            step_id="travel",
            data_schema=self.add_suggested_values_to_schema(
                TRAVEL_SCHEMA, user_input or self._previous_travel
            ),
            errors=errors,
            description_placeholders={
                "name": self._friendly_name(tallest[CONF_ENTITY_ID]),
                "low": f"{low:.4g}",
                "high": f"{high:.4g}",
            },
        )

    def _tallest_spans_group(self, tallest: dict[str, Any]) -> bool:
        """Whether the tallest shade goes lowest and highest, as one roll would."""
        lowest = min(shade[CONF_CLOSED_HEIGHT] for shade in self._shades)
        highest = max(shade[CONF_OPEN_HEIGHT] for shade in self._shades)
        return bool(
            tallest[CONF_CLOSED_HEIGHT] == lowest
            and tallest[CONF_OPEN_HEIGHT] == highest
        )

    def _friendly_name(self, entity_id: str) -> str:
        state = self.hass.states.get(entity_id)
        return (
            str(state.attributes.get(ATTR_FRIENDLY_NAME, entity_id))
            if state
            else entity_id
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
        self._previous_travel = {}

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
            group = _flatten_pico(user_input)
            if error := _validate_group(self.hass, group):
                errors["base"] = error
            else:
                self._name = group.pop(CONF_NAME)
                self._start_shades(group)
                return await self.async_step_shade()

        schema = vol.Schema({vol.Required(CONF_NAME): selector.TextSelector()}).extend(
            _group_schema([], pico_collapsed=True).schema
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
        self._previous_travel = {
            key: options[key]
            for key in (CONF_TRAVEL_TIME_S, CONF_HALFWAY_HEIGHT)
            if key in options
        }
        errors: dict[str, str] = {}
        if user_input is not None:
            group = _flatten_pico(user_input)
            if error := _validate_group(self.hass, group):
                errors["base"] = error
            else:
                self._start_shades(group)
                return await self.async_step_shade()

        own_entities = [
            entry.entity_id
            for entry in er.async_entries_for_config_entry(
                er.async_get(self.hass), self.config_entry.entry_id
            )
        ]
        pico = {key: options[key] for key in PICO_BUTTONS if key in options}
        suggested = user_input or {
            CONF_COVERS: list(self._previous),
            PICO_SECTION: pico,
        }
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                _group_schema(own_entities, pico_collapsed=not pico), suggested
            ),
            errors=errors,
            description_placeholders={"name": self.config_entry.title},
        )

    @callback
    def _async_finish(self, options: dict[str, Any]) -> ConfigFlowResult:
        return self.async_create_entry(data=options)
