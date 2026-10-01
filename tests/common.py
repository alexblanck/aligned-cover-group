"""Test helpers."""

from homeassistant.components.cover import CoverEntityFeature
from homeassistant.const import ATTR_SUPPORTED_FEATURES
from homeassistant.core import HomeAssistant

HIGH_SILL = "cover.high_sill"
LOW_SILL = "cover.low_sill"
PICO_OPEN = "button.pico_on"
PICO_STOP = "button.pico_stop"
PICO_CLOSE = "button.pico_off"

FEATURES = (
    CoverEntityFeature.OPEN
    | CoverEntityFeature.CLOSE
    | CoverEntityFeature.STOP
    | CoverEntityFeature.SET_POSITION
)

SHADES = [
    {"entity_id": HIGH_SILL, "open_height": 84, "closed_height": 24, "travel_time": 30},
    {"entity_id": LOW_SILL, "open_height": 84, "closed_height": 12, "travel_time": 36},
]


def set_shade(hass: HomeAssistant, entity_id: str, position: int) -> None:
    """Set a fake shade's state."""
    hass.states.async_set(
        entity_id,
        "closed" if position == 0 else "open",
        {"current_position": position, ATTR_SUPPORTED_FEATURES: FEATURES},
    )
