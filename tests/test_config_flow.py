"""Config flow validation errors (the happy paths run in test_room.py)."""

from homeassistant import config_entries
from homeassistant.core import HomeAssistant

from custom_components.aligned_cover_group.const import DOMAIN

from .common import (
    HIGH_SILL,
    LOW_SILL,
    PICO_OPEN,
    set_shade,
)


async def test_group_errors(hass: HomeAssistant) -> None:
    set_shade(hass, HIGH_SILL, 0)
    set_shade(hass, LOW_SILL, 0)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "x", "covers": [HIGH_SILL]}
    )
    assert result["errors"] == {"base": "too_few_covers"}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"name": "x", "covers": [HIGH_SILL, LOW_SILL], "pico_open": PICO_OPEN},
    )
    assert result["errors"] == {"base": "pico_incomplete"}

    hass.states.async_set(HIGH_SILL, "open", {"supported_features": 3})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "x", "covers": [HIGH_SILL, LOW_SILL]}
    )
    assert result["errors"] == {"base": "cover_no_position"}


async def test_shade_heights_validated(hass: HomeAssistant) -> None:
    set_shade(hass, HIGH_SILL, 0)
    set_shade(hass, LOW_SILL, 0)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "x", "covers": [HIGH_SILL, LOW_SILL]}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"open_height": 10, "closed_height": 20, "travel_time_s": 30}
    )
    assert result["errors"] == {"base": "closed_not_below_open"}
    assert result["description_placeholders"]["index"] == "1"
