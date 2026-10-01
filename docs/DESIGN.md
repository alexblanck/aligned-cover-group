# Aligned Cover Group — design

A Home Assistant custom integration (HACS-installable) that groups side-by-side
window shades of different sizes into a single `cover` entity whose bottom edges
("hemlines") line up — both at rest and while moving.

## Goals

1. **Alignment** — shades of different heights/positions present an even
   hemline. The group's position is a hemline height, not a shared percentage.
2. **Synchronized motion** — when a Lutron Pico is paired (on the Caseta bridge)
   to exactly the shades in the group, the group uses Pico button presses so
   every shade starts/stops at the same instant.

Pico support is optional; without one the group still aligns shades using
per-shade commands.

## Configuration (UI only)

Created via a config flow, edited via an options flow. No YAML.

Group:

| Field | Notes |
|---|---|
| name | |
| covers | 2+ cover entities that support `set_position` |
| pico open / stop / close buttons | optional `button` entities (all three or none) |

Per shade:

| Field | Notes |
|---|---|
| open_height | hemline height when fully open |
| closed_height | hemline height when fully closed (< open_height) |
| travel_time | seconds for full close→open travel |

Heights use any unit, as long as every shade uses the same reference (e.g.
inches from the floor). Tops do not need to match.

A single travel time per shade is used, assuming each shade moves at the same
speed up and down (confirmed on the Caseta shades this was built for). Separate
up/down times can be added if other hardware needs them.

## Position math

- Group range: lowest `closed_height` (0%) to highest `open_height` (100%).
- Group position → hemline `H` → each shade's position:
  `clamp((H - closed) / (open - closed) * 100, 0, 100)`.
- Reported position: if the shades are *aligned* (one hemline `H` is consistent
  with every shade, treating a fully closed/open shade as consistent with any
  `H` beyond its end), report that `H`. Otherwise report the average hemline.
  This degrades to the average (like HA's cover group) while keeping the slider
  stable after the group itself moved the shades.

## Motion

Commands are open-loop: Caseta shades don't reliably report position while
moving, so timing comes from `travel_time`. While the group's own motion is
running, a new command plans from *estimated* positions (start position, start
time, speed) rather than the last reported ones, so reversing or retargeting
mid-move works even if shades only report when they stop.

**Pico path** — used when a Pico is configured *and* every shade the Pico would
move (all shades not already at the endpoint in the direction of travel) starts
from the same hemline. Press Pico open/close, then immediately send
`set_position` to shades whose target is short of the endpoint.

**Staggered path** — otherwise. Shades moving up start in order from lowest
hemline; each one starts when the leader's estimated hemline reaches it (and
vice versa for moving down). Implemented as delayed `set_position` calls.

**Stop** — cancel any pending staggered starts, then:
- Pico configured and the group believes it is moving → press Pico stop.
- Otherwise → `stop_cover` on each shade.

Caveat to verify: on a shade Pico the middle button means "stop" while moving
but "go to favorite" when stationary, so the Pico stop is only pressed while
the group thinks it's moving.

## Out of scope (v1)

Tilt, non-linear calibration, YAML config, non-Lutron remotes, multiple presets,
separate up/down speeds.
