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
| covers | 2+ cover entities that support `set_position` and `stop` |
| pico open / stop / close buttons | optional Lutron Caseta `button` entities (all three, distinct, or none) |
| travel_time_s | seconds for the tallest shade's full close→open travel |

Per shade:

| Field | Notes |
|---|---|
| closed_height | hemline height when fully closed |
| open_height | hemline height when fully open (> closed_height) |

Heights use any unit, as long as every shade uses the same reference (e.g.
inches from the floor). Tops do not need to match, but every shade's range must
overlap the others' (side-by-side windows, not stacked ones): with no shared
height, "level" has no meaning.

Every shade is assumed to move at one shared speed (height units per second),
the same up and down: hemlines can only stay level while moving if they move
at the same speed. The speed comes from the tallest shade (the most accurate to
time), and each shade's travel time is derived from its own range. Shades with
different speeds still end level, since each stops at its own target, but
drift apart mid-move.

## Position math

- Group range: lowest `closed_height` (0%) to highest `open_height` (100%).
- Group position → hemline `H` → each shade's position:
  `clamp((H - closed) / (open - closed) * 100, 0, 100)`.
- Aligned: there's a group hemline height `H` that would put every shade where
  it is (each shade's hemline within 1% of the group's range of `H` clamped to
  that shade's range). `H` is the average hemline of the shades that are
  partway, or the group's closed/open height when every shade is fully
  closed/open.
- Reported position: `H` when aligned, otherwise the average hemline. This
  degrades to the average (like HA's cover group) while keeping the slider
  stable after the group itself moved the shades.

## Motion

Commands are open-loop: Caseta shades don't reliably report position while
moving (nor opening/closing), so timing comes from the shared speed. While the
group's own motion is running, a new command plans from *estimated* positions
(start position, start time, speed) rather than the last reported ones, so
reversing or retargeting mid-move works even if shades only report when they
stop. A shade still on an earlier trip that already sits at its new target is
sent a command to hold there, otherwise it would carry on to its old target.

**Pico path** — used when a Pico is configured *and* every shade the Pico would
move (all shades not already at the endpoint in the direction of travel) starts
from the same hemline. Press Pico open/close, then immediately send
`set_position` to shades whose target is short of the endpoint.

**Staggered path** — otherwise. Shades moving up start in order from lowest
hemline; each one starts when the leader's estimated hemline reaches it (and
vice versa for moving down). Implemented as delayed `set_position` calls.

Delayed starts and the end of the move are scheduled at absolute times from
when the move began, so slow commands to the bridge don't push later starts
back. A move ends when every shade has reported reaching its target; if the
planned end (plus a 2 s margin) passes first, the group stops treating the
shades as moving and logs a warning naming the late shades. If a starting
command fails, the move is abandoned and the error returned to the caller.

**Stop** — cancel any pending staggered starts, then:
- Pico configured and the group believes it is moving → press Pico stop.
- Otherwise → `stop_cover` on each shade.

Caveat to verify: on a shade Pico the middle button means "stop" while moving
but "go to favorite" when stationary, so the Pico stop is only pressed while
the group thinks it's moving.

## Out of scope (v1)

Tilt, non-linear calibration, YAML config, non-Lutron remotes, multiple presets,
separate up/down speeds.
