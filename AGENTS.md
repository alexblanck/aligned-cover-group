# Agent instructions

@DEVELOPMENT.md

See [docs/DESIGN.md](docs/DESIGN.md) for how the integration works.

- Run all three checks from DEVELOPMENT.md (pytest, ruff, `mypy --strict`)
  before reporting a change as done.
- Prefer end-to-end scenarios in `tests/test_room.py` over small unit tests.
- Don't commit or push unless asked.
