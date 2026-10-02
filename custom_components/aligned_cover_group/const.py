"""Constants for Aligned Cover Group."""

DOMAIN = "aligned_cover_group"

CONF_COVERS = "covers"
CONF_OPEN_HEIGHT = "open_height"
CONF_CLOSED_HEIGHT = "closed_height"
CONF_TRAVEL_TIME_S = "travel_time_s"
# The tallest shade's hemline height at 50%, describing how its roll curves.
CONF_HALFWAY_HEIGHT = "halfway_height"
CONF_PICO_OPEN = "pico_open"
CONF_PICO_STOP = "pico_stop"
CONF_PICO_CLOSE = "pico_close"

PICO_BUTTONS = (CONF_PICO_OPEN, CONF_PICO_STOP, CONF_PICO_CLOSE)
# Form section holding the Pico buttons; stored flattened in the options.
PICO_SECTION = "pico"
