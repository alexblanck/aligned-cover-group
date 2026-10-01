"""Config flow validation errors (the happy paths run in test_room.py)."""

from typing import Any

from homeassistant import config_entries
from homeassistant.components.cover import CoverEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.aligned_cover_group.const import DOMAIN

from .common import (
    HIGH_SILL,
    LOW_SILL,
    PICO_CLOSE,
    PICO_OPEN,
    PICO_STOP,
    set_shade,
)


async def start_flow(hass: HomeAssistant) -> dict[str, Any]:
    set_shade(hass, HIGH_SILL, 0)
    set_shade(hass, LOW_SILL, 0)
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def submit_group(
    hass: HomeAssistant, flow: dict[str, Any], **extra: Any
) -> dict[str, Any]:
    return await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"name": "x", "covers": [HIGH_SILL, LOW_SILL], **extra}
    )


async def test_too_few_covers(hass: HomeAssistant) -> None:
    flow = await start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"name": "x", "covers": [HIGH_SILL]}
    )
    assert result["errors"] == {"base": "too_few_covers"}


async def test_pico_buttons_all_or_none_and_distinct(hass: HomeAssistant) -> None:
    flow = await start_flow(hass)
    result = await submit_group(hass, flow, pico_open=PICO_OPEN)
    assert result["errors"] == {"base": "pico_incomplete"}

    result = await submit_group(
        hass, flow, pico_open=PICO_OPEN, pico_stop=PICO_OPEN, pico_close=PICO_CLOSE
    )
    assert result["errors"] == {"base": "pico_duplicate"}

    result = await submit_group(
        hass, flow, pico_open=PICO_OPEN, pico_stop=PICO_STOP, pico_close=PICO_CLOSE
    )
    assert result["step_id"] == "shade"


async def test_covers_must_set_position_and_stop(hass: HomeAssistant) -> None:
    flow = await start_flow(hass)
    hass.states.async_set(
        HIGH_SILL, "open", {"supported_features": CoverEntityFeature.SET_POSITION}
    )
    result = await submit_group(hass, flow)
    assert result["errors"] == {"base": "cover_unsupported"}


async def test_unavailable_cover_checked_through_the_registry(
    hass: HomeAssistant,
) -> None:
    entry = er.async_get(hass).async_get_or_create(
        "cover",
        "test",
        "high_sill",
        suggested_object_id="high_sill",
        supported_features=CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE,
    )
    assert entry.entity_id == HIGH_SILL
    flow = await start_flow(hass)
    hass.states.async_set(HIGH_SILL, "unavailable")
    result = await submit_group(hass, flow)
    assert result["errors"] == {"base": "cover_unsupported"}


async def test_shade_heights_validated(hass: HomeAssistant) -> None:
    flow = await start_flow(hass)
    result = await submit_group(hass, flow)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"open_height": 10, "closed_height": 20}
    )
    assert result["errors"] == {"base": "closed_not_below_open"}
    assert result["description_placeholders"]["index"] == "1"


async def test_shade_ranges_must_overlap(hass: HomeAssistant) -> None:
    flow = await start_flow(hass)
    result = await submit_group(hass, flow)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"open_height": 40, "closed_height": 10}
    )
    # Stacked rather than side by side: no height is in both ranges.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"open_height": 80, "closed_height": 50}
    )
    assert result["errors"] == {"base": "ranges_do_not_overlap"}
    assert result["description_placeholders"]["index"] == "2"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"open_height": 80, "closed_height": 20}
    )
    assert result["step_id"] == "travel"
