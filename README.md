# Aligned Cover Group

A Home Assistant helper that groups side-by-side window shades of different
sizes into one cover whose bottom edges (hemlines) stay level — at rest and
while moving. Optionally drives a Lutron Caseta Pico so every shade starts and
stops at exactly the same moment.

## How it works

- You tell it each shade's hemline height when fully open and fully closed,
  measured from a common reference (e.g. inches from the floor), plus how long
  it takes to travel fully.
- The group's 0–100% position is a shared hemline height. Each shade is sent
  to whatever position puts its hemline there (clamped to its own range).
- When shades start from different heights, the lowest (or highest) one starts
  first and the others join as its hemline reaches theirs.
- With a Pico configured, aligned moves start with a Pico press and stops are
  always sent through the Pico, so the bridge moves every shade in lockstep.

See [docs/DESIGN.md](docs/DESIGN.md) for details.

## Installation (HACS)

1. HACS → ⋮ → Custom repositories → add this repository as an **Integration**.
2. Install **Aligned Cover Group** and restart Home Assistant.
3. Settings → Devices & services → Helpers → Create helper → **Aligned Cover Group**.

## Pico setup (optional)

1. In the Lutron app, pair a Pico to **exactly** the shades in the group.
2. In Home Assistant, open the Pico's device (Lutron Caseta integration) and
   enable its disabled button entities.
3. Choose its Up, Stop and Down buttons when creating the group.

## Troubleshooting

Turn on debug logging to see each move the group plans: whether it used the
Pico (and why not), every shade command with its delay, and stops.

```yaml
logger:
  logs:
    custom_components.aligned_cover_group: debug
```

## Development

See [DEVELOPMENT.md](DEVELOPMENT.md).

## Credits

Designed, developed and tested with assistance from
[Claude](https://claude.com/claude-code) (Anthropic), working with
@alexblanck.

## License

[MIT](LICENSE)
