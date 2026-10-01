"""End-to-end scenarios: a user drives the group; simulated shades move over time."""

import logging

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant

from .sim import FAVORITE, ShadeSpec, build_room

# Hemline spread allowed while moving, in inches. The HA test harness fires
# async_call_later timers up to 0.5 s early under frozen time (production HA
# schedules them exactly); at 2 in/s that's 1 in, plus position rounding.
HEIGHT_TOLERANCE = 1.5

# Same top, different sills, same speed (2 in/s).
HIGH_SILL = "cover.high_sill"
LOW_SILL = "cover.low_sill"


def same_tops(position_pct: int = 0) -> list[ShadeSpec]:
    return [
        ShadeSpec("high_sill", 24, 84, 30, position_pct),
        ShadeSpec("low_sill", 12, 84, 36, position_pct),
    ]


both_reporting_modes = pytest.mark.parametrize(
    "report_while_moving", [True, False], ids=["reports-moving", "reports-at-rest"]
)


@both_reporting_modes
@pytest.mark.parametrize("pico", [True, False], ids=["pico", "no-pico"])
async def test_open_from_closed_stays_aligned(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    pico: bool,
    report_while_moving: bool,
) -> None:
    room = await build_room(hass, freezer, same_tops(0), pico, report_while_moving)
    assert room.group.state == "closed"

    await room.command("open_cover")
    assert room.group.state == "opening"
    await room.run_until_still()

    assert room.positions_pct() == {HIGH_SILL: 100, LOW_SILL: 100}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE
    # The high-sill shade waited until the other's hemline reached its sill.
    low_start = room[LOW_SILL].starts[0][0]
    high_start = room[HIGH_SILL].starts[0][0]
    assert high_start - low_start == pytest.approx(6, abs=0.5)
    assert room.group.state == "open"
    assert room.group.attributes["current_position"] == 100
    if pico:
        # Different sills: the Pico would have started both at once.
        assert room.pico["open"].presses == 0


@both_reporting_modes
async def test_close_from_open_uses_pico_in_lockstep(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, report_while_moving: bool
) -> None:
    room = await build_room(hass, freezer, same_tops(100), True, report_while_moving)

    await room.command("close_cover")
    await room.run_until_still()

    assert room.pico["close"].presses == 1
    assert room[HIGH_SILL].starts[0][0] == room[LOW_SILL].starts[0][0]
    assert room.positions_pct() == {HIGH_SILL: 0, LOW_SILL: 0}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE
    assert room.group.state == "closed"


@both_reporting_modes
async def test_partial_close_then_reopen(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, report_while_moving: bool
) -> None:
    room = await build_room(hass, freezer, same_tops(100), True, report_while_moving)

    await room.command("set_cover_position", position=50)
    await room.run_until_still()
    # Hemline 48: (48 - 24) / 60 and (48 - 12) / 72.
    assert room.positions_pct() == {HIGH_SILL: 40, LOW_SILL: 50}
    assert room.group.attributes["current_position"] == 50
    assert room.group.attributes["aligned"] is True

    await room.command("open_cover")
    await room.run_until_still()
    assert room.positions_pct() == {HIGH_SILL: 100, LOW_SILL: 100}
    assert room.pico["close"].presses == 1
    assert room.pico["open"].presses == 1
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE


@both_reporting_modes
async def test_stop_during_staggered_open(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, report_while_moving: bool
) -> None:
    room = await build_room(hass, freezer, same_tops(0), True, report_while_moving)

    await room.command("open_cover")
    await room.run(3)  # low-sill shade is moving; high-sill hasn't started
    await room.command("stop_cover")
    await room.run(20)

    assert room.pico["stop"].presses == 1
    assert not room[HIGH_SILL].starts, "pending start fired after stop"
    assert room.positions_pct()[LOW_SILL] == pytest.approx(8, abs=1)
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE
    assert room.group.state not in ("opening", "closing")
    assert room.group.attributes["aligned"] is True


@both_reporting_modes
async def test_stop_while_aligned_and_moving(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, report_while_moving: bool
) -> None:
    room = await build_room(hass, freezer, same_tops(100), True, report_while_moving)

    await room.command("close_cover")
    await room.run(10)
    await room.command("stop_cover")
    await room.run(20)

    # Both stopped by the same Pico press, 20 in down from the top.
    assert room.positions_pct() == {HIGH_SILL: 67, LOW_SILL: 72}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE
    assert room.group.attributes["current_position"] == 72


async def test_stop_while_idle_does_not_trigger_favorite(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(100))

    await room.command("stop_cover")
    await room.run(30)

    assert room.pico["stop"].presses == 0
    assert room.positions_pct() == {HIGH_SILL: 100, LOW_SILL: 100}
    assert all(shade.position_pct != FAVORITE for shade in room.shades.values())


@both_reporting_modes
async def test_reverse_while_opening(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, report_while_moving: bool
) -> None:
    room = await build_room(hass, freezer, same_tops(0), True, report_while_moving)

    await room.command("open_cover")
    await room.run(15)  # both shades moving up
    await room.command("close_cover")
    await room.run_until_still()

    assert room.positions_pct() == {HIGH_SILL: 0, LOW_SILL: 0}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE


@both_reporting_modes
async def test_retarget_while_moving(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, report_while_moving: bool
) -> None:
    room = await build_room(hass, freezer, same_tops(100), True, report_while_moving)

    await room.command("close_cover")
    await room.run(5)
    await room.command("set_cover_position", position=50)
    await room.run_until_still()

    assert room.positions_pct() == {HIGH_SILL: 40, LOW_SILL: 50}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE


async def test_different_tops_and_sills(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    # Shades offset vertically, same speed (2 in/s).
    room = await build_room(
        hass,
        freezer,
        [ShadeSpec("left", 10, 60, 25, 0), ShadeSpec("right", 20, 80, 30, 0)],
    )

    await room.command("set_cover_position", position=50)  # hemline 45
    await room.run_until_still()
    assert room.positions_pct() == {"cover.left": 70, "cover.right": 42}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE

    await room.command("open_cover")
    await room.run_until_still()
    assert room.positions_pct() == {"cover.left": 100, "cover.right": 100}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE
    assert room.group.attributes["current_position"] == 100

    await room.command("close_cover")
    await room.run_until_still()
    assert room.positions_pct() == {"cover.left": 0, "cover.right": 0}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE


async def test_options_change_applies_to_running_group(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(100))

    # Remove the Pico through the options flow; the group reloads without it.
    flow = await hass.config_entries.options.async_init(room.entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"covers": [HIGH_SILL, LOW_SILL]}
    )
    for shade in room.shades.values():
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"],
            {
                "open_height": shade.spec.open_height,
                "closed_height": shade.spec.closed_height,
                "travel_time_s": shade.spec.travel_time_s,
            },
        )
    assert flow["type"] == "create_entry", flow
    await hass.async_block_till_done()

    await room.command("close_cover")
    await room.run_until_still()
    assert room.pico["close"].presses == 0
    assert room.positions_pct() == {HIGH_SILL: 0, LOW_SILL: 0}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE


async def test_unavailable_shade(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    room = await build_room(hass, freezer, same_tops(0))
    room[HIGH_SILL].set_available(False)
    await hass.async_block_till_done()

    # The remaining shade alone decides the group's state.
    assert room.group.state == "closed"
    assert room.group.attributes["current_position"] == 0

    with caplog.at_level(logging.WARNING):
        await room.command("set_cover_position", position=50)
    await room.run_until_still()
    assert f"leaving out shades with no position: {HIGH_SILL}" in caplog.text
    # Positions are unknown, so the Pico (which would move both) isn't used.
    assert room.pico["open"].presses == 0
    assert room.positions_pct() == {HIGH_SILL: 0, LOW_SILL: 50}
    assert room.group.attributes["current_position"] == 50

    # It comes back where it was, now out of line with the other shade.
    room[HIGH_SILL].set_available(True)
    await hass.async_block_till_done()
    assert room.group.attributes["aligned"] is False

    await room.command("set_cover_position", position=50)
    await room.run_until_still()
    assert room.positions_pct() == {HIGH_SILL: 40, LOW_SILL: 50}
    assert room.group.attributes["aligned"] is True


async def test_all_shades_unavailable(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(0))
    for shade in room.shades.values():
        shade.set_available(False)
    await hass.async_block_till_done()
    assert room.group.state == "unavailable"

    room[LOW_SILL].set_available(True)
    await hass.async_block_till_done()
    assert room.group.state == "closed"
