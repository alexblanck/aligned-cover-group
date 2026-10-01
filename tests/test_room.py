"""End-to-end scenarios: a user drives the group; simulated shades move over time."""

import asyncio
import logging

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .common import HIGH_SILL, LOW_SILL, SHADES, SPEED
from .sim import FAVORITE, ShadeSpec, build_room

# Hemline spread allowed while moving, in inches: timers fire on the next
# 0.25 s tick (0.5 in at 2 in/s), plus whole-percent position rounding.
HEIGHT_TOLERANCE = 1.0


def same_tops(position_pct: int = 0) -> list[ShadeSpec]:
    return [ShadeSpec.from_config(config, SPEED, position_pct) for config in SHADES]


@pytest.mark.parametrize("pico", [True, False], ids=["pico", "no-pico"])
async def test_open_from_closed_stays_aligned(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    pico: bool,
) -> None:
    room = await build_room(hass, freezer, same_tops(0), pico)
    assert room.group.state == "closed"

    await room.command("open_cover")
    assert room.group.state == "opening"
    await room.run_until_still()

    assert room.positions_pct_by_id() == {HIGH_SILL: 100, LOW_SILL: 100}
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


async def test_close_from_open_uses_pico_in_lockstep(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(100), True)

    await room.command("close_cover")
    await room.run_until_still()

    assert room.pico["close"].presses == 1
    assert room[HIGH_SILL].starts[0][0] == room[LOW_SILL].starts[0][0]
    assert room.positions_pct_by_id() == {HIGH_SILL: 0, LOW_SILL: 0}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE
    assert room.group.state == "closed"


async def test_partial_close_then_reopen(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(100), True)

    await room.command("set_cover_position", position=50)
    await room.run_until_still()
    # Hemline 48: (48 - 24) / 60 and (48 - 12) / 72.
    assert room.positions_pct_by_id() == {HIGH_SILL: 40, LOW_SILL: 50}
    assert room.group.attributes["current_position"] == 50
    assert room.group.attributes["aligned"] is True
    assert room.group.attributes["hemline_heights"] == {HIGH_SILL: 48, LOW_SILL: 48}

    await room.command("open_cover")
    await room.run_until_still()
    assert room.positions_pct_by_id() == {HIGH_SILL: 100, LOW_SILL: 100}
    assert room.pico["close"].presses == 1
    assert room.pico["open"].presses == 1
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE


async def test_stop_during_staggered_open(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(0), True)

    await room.command("open_cover")
    await room.run(3)  # low-sill shade is moving; high-sill hasn't started
    await room.command("stop_cover")
    await room.run(20)

    assert room.pico["stop"].presses == 1
    assert not room[HIGH_SILL].starts, "pending start fired after stop"
    assert room.positions_pct_by_id()[LOW_SILL] == pytest.approx(8, abs=1)
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE
    assert room.group.state not in ("opening", "closing")
    assert room.group.attributes["aligned"] is True


async def test_stop_while_aligned_and_moving(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(100), True)

    await room.command("close_cover")
    await room.run(10)
    await room.command("stop_cover")
    await room.run(20)

    # Both stopped by the same Pico press, 20 in down from the top.
    assert room.positions_pct_by_id() == {HIGH_SILL: 67, LOW_SILL: 72}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE
    assert room.group.attributes["current_position"] == 72


async def test_stop_while_idle_does_not_trigger_favorite(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(100))

    await room.command("stop_cover")
    await room.run(30)

    assert room.pico["stop"].presses == 0
    assert room.positions_pct_by_id() == {HIGH_SILL: 100, LOW_SILL: 100}
    assert all(shade.position_pct != FAVORITE for shade in room.shades.values())


async def test_reverse_while_opening(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(0), True)

    await room.command("open_cover")
    await room.run(15)  # both shades moving up
    await room.command("close_cover")
    await room.run_until_still()

    assert room.positions_pct_by_id() == {HIGH_SILL: 0, LOW_SILL: 0}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE


async def test_retarget_while_moving(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(100), True)

    await room.command("close_cover")
    await room.run(5)
    await room.command("set_cover_position", position=50)
    await room.run_until_still()

    assert room.positions_pct_by_id() == {HIGH_SILL: 40, LOW_SILL: 50}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE


@pytest.mark.parametrize("pico", [True, False], ids=["pico", "no-pico"])
async def test_different_tops_and_sills(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    pico: bool,
) -> None:
    # Shades offset vertically, same speed (2 in/s).
    room = await build_room(
        hass,
        freezer,
        [ShadeSpec("left", 10, 60, 25, 0), ShadeSpec("right", 20, 80, 30, 0)],
        pico,
    )

    await room.command("set_cover_position", position=50)  # hemline 45
    await room.run_until_still()
    assert room.positions_pct_by_id() == {"cover.left": 70, "cover.right": 42}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE

    await room.command("open_cover")
    await room.run_until_still()
    assert room.positions_pct_by_id() == {"cover.left": 100, "cover.right": 100}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE
    assert room.group.attributes["current_position"] == 100

    await room.command("close_cover")
    await room.run_until_still()
    assert room.positions_pct_by_id() == {"cover.left": 0, "cover.right": 0}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE


async def test_options_change_applies_to_running_group(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(100))

    # Remove the Pico through the options flow; the group reloads without it.
    flow = await hass.config_entries.options.async_init(room.entry.entry_id)
    # The Pico section opens expanded, since this group has one.
    pico_section = flow["data_schema"].schema["pico"]
    assert pico_section.options["collapsed"] is False
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"covers": [HIGH_SILL, LOW_SILL], "pico": {}}
    )
    flow = await room.answer_shade_steps(
        flow, hass.config_entries.options.async_configure
    )
    assert flow["type"] == "create_entry", flow
    await hass.async_block_till_done()

    await room.command("close_cover")
    await room.run_until_still()
    assert room.pico["close"].presses == 0
    assert room.positions_pct_by_id() == {HIGH_SILL: 0, LOW_SILL: 0}
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
    assert room.positions_pct_by_id() == {HIGH_SILL: 0, LOW_SILL: 50}
    assert room.group.attributes["current_position"] == 50
    assert room.group.attributes["hemline_heights"] == {LOW_SILL: 48}

    # It comes back where it was, now out of line with the other shade.
    room[HIGH_SILL].set_available(True)
    await hass.async_block_till_done()
    assert room.group.attributes["aligned"] is False

    await room.command("set_cover_position", position=50)
    await room.run_until_still()
    assert room.positions_pct_by_id() == {HIGH_SILL: 40, LOW_SILL: 50}
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


async def test_shades_moving_opposite_ways(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    # Out of line: hemlines at 54 and 48 (group at 54%).
    room = await build_room(hass, freezer, same_tops(50), True)
    assert room.group.attributes["aligned"] is False
    assert room.group.attributes["current_position"] == 54

    # Hemline 52.3: the high-sill shade goes down, the low-sill shade goes up,
    # and the group's own position goes up.
    await room.command("set_cover_position", position=56)
    assert room.group.state == "opening"
    assert room.pico["open"].presses == room.pico["close"].presses == 0
    await room.run_until_still()

    assert room.positions_pct_by_id() == {HIGH_SILL: 47, LOW_SILL: 56}
    assert room.group.attributes["aligned"] is True
    assert room.group.attributes["current_position"] == 56


@pytest.mark.parametrize("pico", [True, False], ids=["pico", "no-pico"])
@pytest.mark.parametrize(
    ("service", "end_pct", "end_state"),
    [("open_cover", 100, "open"), ("close_cover", 0, "closed")],
    ids=["open", "close"],
)
@pytest.mark.parametrize(
    "start_pcts",
    [(0, 100, 50), (100, 0, 0), (30, 70, 10), (99, 1, 50)],
    ids=lambda pcts: "-".join(map(str, pcts)),
)
async def test_open_and_close_from_shuffled_positions(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    start_pcts: tuple[int, int, int],
    service: str,
    end_pct: int,
    end_state: str,
    pico: bool,
) -> None:
    # Three shades of different sizes and speeds, starting out of line.
    specs = [
        ShadeSpec("a", 24, 84, 30, start_pcts[0]),
        ShadeSpec("b", 12, 84, 36, start_pcts[1]),
        ShadeSpec("c", 30, 72, 15, start_pcts[2]),
    ]
    room = await build_room(hass, freezer, specs, pico)

    await room.command(service)
    await room.run_until_still()

    assert set(room.positions_pct_by_id().values()) == {end_pct}
    assert room.group.state == end_state
    assert room.group.attributes["current_position"] == end_pct
    assert room.group.attributes["aligned"] is True


@pytest.mark.parametrize("pico", [True, False], ids=["pico", "no-pico"])
async def test_retarget_to_where_the_shades_are(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    pico: bool,
) -> None:
    room = await build_room(hass, freezer, same_tops(100), pico)

    await room.command("close_cover")
    await room.run(10)  # hemlines at 64: low-sill shade at 72%
    await room.command("set_cover_position", position=72)
    await room.run_until_still()

    # Both stop here rather than carrying on to closed.
    assert room.positions_pct_by_id() == {HIGH_SILL: 66, LOW_SILL: 72}
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE
    assert room.group.attributes["current_position"] == 72


@pytest.mark.parametrize("pico", [True, False], ids=["pico", "no-pico"])
async def test_staggered_start_with_slow_commands(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    pico: bool,
) -> None:
    room = await build_room(hass, freezer, same_tops(0), pico)
    room.bridge.latency_s = 1.0

    await room.command("open_cover")
    await room.run_until_still()

    # Each shade starts a second after its command; the gap between them must
    # still be what the hemlines need (12 in at 2 in/s).
    low_start = room[LOW_SILL].starts[0][0]
    high_start = room[HIGH_SILL].starts[0][0]
    assert high_start - low_start == pytest.approx(6, abs=0.5)
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE
    assert room.positions_pct_by_id() == {HIGH_SILL: 100, LOW_SILL: 100}


@pytest.mark.parametrize("pico", [True, False], ids=["pico", "no-pico"])
async def test_retarget_while_a_staggered_start_is_pending(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    pico: bool,
) -> None:
    room = await build_room(hass, freezer, same_tops(0), pico)

    await room.command("open_cover")
    await room.run(3)  # low-sill shade moving; high-sill shade not started yet
    await room.command("set_cover_position", position=50)
    await room.run_until_still()

    assert room.positions_pct_by_id() == {HIGH_SILL: 40, LOW_SILL: 50}
    # The original start (to 100%) never fired; only the new one did.
    assert [target for _, target in room[HIGH_SILL].starts] == [40]
    assert room.worst_misalignment() <= HEIGHT_TOLERANCE


async def test_new_command_while_the_first_commands_are_in_flight(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(0), pico=False)
    room.bridge.gate = asyncio.Event()

    # Opening: the low-sill shade's command is held up at the bridge.
    opening = hass.async_create_task(room.command("open_cover"))
    for _ in range(5):
        await asyncio.sleep(0)
    closing = hass.async_create_task(room.command("close_cover"))
    for _ in range(5):
        await asyncio.sleep(0)
    room.bridge.gate.set()
    await opening
    await closing
    await room.run_until_still()

    # The opening plan's delayed start for the high-sill shade must not fire.
    assert room[HIGH_SILL].starts == []
    assert room.positions_pct_by_id() == {HIGH_SILL: 0, LOW_SILL: 0}


async def test_failed_command(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(0))
    room.bridge.fail = True

    with pytest.raises(HomeAssistantError):
        await room.command("open_cover")
    assert room.group.state == "closed"

    # Nothing is moving, so a stop mustn't press the Pico (that would send
    # the shades to their favorite position).
    room.bridge.fail = False
    await room.command("stop_cover")
    await room.run(10)
    assert room.pico["stop"].presses == 0
    assert room.positions_pct_by_id() == {HIGH_SILL: 0, LOW_SILL: 0}


async def test_options_change_mid_move(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(0))

    await room.command("open_cover")
    await room.run(3)  # high-sill shade's start still pending
    flow = await hass.config_entries.options.async_init(room.entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"covers": [HIGH_SILL, LOW_SILL], "pico": {}}
    )
    flow = await room.answer_shade_steps(
        flow, hass.config_entries.options.async_configure
    )
    await hass.async_block_till_done()
    await room.run(20)

    # The reloaded group forgot the old plan, including its pending start.
    assert room[HIGH_SILL].starts == []
    assert room.group.state != "opening"


async def test_group_has_its_own_service_device(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(0))

    entity = er.async_get(hass).async_get("cover.living_room")
    device = dr.async_get(hass).async_get(entity.device_id)
    assert device.name == "Living Room"
    assert device.entry_type is dr.DeviceEntryType.SERVICE
    assert device.config_entries == {room.entry.entry_id}


async def test_motion_ends_at_the_planned_end(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    # Configured slower than the shades really are. They report their
    # destination immediately, so the group can't tell they arrived early.
    room = await build_room(hass, freezer, same_tops(100), configured_travel_time_s=45)

    await room.command("close_cover")
    await room.run(40)  # shades closed at 36 s
    assert room.positions_pct_by_id() == {HIGH_SILL: 0, LOW_SILL: 0}
    assert room.group.state == "closing"

    await room.run(8)  # planned end 45 s, plus margin
    assert room.group.state == "closed"
    await room.command("stop_cover")
    await room.run(10)
    assert room.pico["stop"].presses == 0
    assert room.positions_pct_by_id() == {HIGH_SILL: 0, LOW_SILL: 0}


async def test_outside_command_takes_over(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    room = await build_room(hass, freezer, same_tops(0), pico=False)

    await room.command("open_cover")
    await room.run(3)  # high-sill shade's start still pending
    with caplog.at_level(logging.INFO):
        # Someone else sends the low-sill shade somewhere.
        await hass.services.async_call(
            "cover",
            "set_cover_position",
            {"entity_id": LOW_SILL, "position": 20},
            blocking=True,
        )
    assert "another command took over" in caplog.text
    assert room.group.state != "opening"

    await room.run(20)
    assert room[HIGH_SILL].starts == []
    assert room.positions_pct_by_id()[LOW_SILL] == 20


async def test_physical_pico_stop_takes_over(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    room = await build_room(hass, freezer, same_tops(100))

    await room.command("close_cover")
    await room.run(10)
    # Someone presses Stop on the Pico itself.
    await hass.services.async_call(
        "button", "press", {"entity_id": room.pico["stop"].entity_id}, blocking=True
    )
    await room.run(1)

    assert room.group.state not in ("opening", "closing")
    assert room.positions_pct_by_id() == {HIGH_SILL: 67, LOW_SILL: 72}
