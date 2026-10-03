"""Tests for the controller state machine fixes from the v1.3.1 logic review.

Covers:
  - transient 'unavailable'/'unknown' charger status does not lose the recovery (#1)
  - no timers / writes after the controller is stopped or reloaded (#2)
  - anti-flapping: stop_delay_s, start_delay_s, start_hysteresis_w (#3, #4)
  - toggle-button retry with cooldown and attempt limit (#5, #6)
  - start/stop button ignored without charger_status_entity (#7)
  - override actions bypass min_delta_amp (#8)
  - enabling override restarts a charger we stopped (#9)
  - computed-current sensor drops to 0 when charging stops
  - non-numeric voltage falls back to 230 V in the recovery timer too

Run with:
    python -m pytest tests/test_state_machine.py -v
"""

from __future__ import annotations

import asyncio
import types
import unittest.mock as mock
from unittest.mock import MagicMock

import pytest

from tests.conftest import FakeState, make_controller


def status_event(old: str | None, new: str | None):
    return types.SimpleNamespace(data={
        "old_state": FakeState(old) if old is not None else None,
        "new_state": FakeState(new) if new is not None else None,
    })


def use_clock(ctrl, start: float = 1000.0) -> list[float]:
    """Replace the controller's monotonic clock; mutate clock[0] to advance time."""
    clock = [start]
    ctrl._monotonic = lambda: clock[0]
    return clock


def set_power(ctrl, hass, value: float) -> None:
    """Change the grid power like HA does: update the state AND fire the state-change event."""
    old = hass.states.get("sensor.grid_power")
    hass.states.set("sensor.grid_power", value)
    ctrl._handle_power_change(types.SimpleNamespace(data={
        "old_state": old,
        "new_state": FakeState(str(value)),
    }))


async def drain() -> None:
    """Let tasks created via hass.async_create_task run to completion."""
    for _ in range(5):
        await asyncio.sleep(0)


def charging_states(grid_w: float, status: str = "Charging") -> dict:
    return {
        "sensor.grid_power": grid_w,
        "sensor.grid_voltage": 230,
        "sensor.charger_status": status,
    }


# ---------------------------------------------------------------------------
# #1 – transient unavailable status
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unavailable_blip_keeps_recovery_armed():
    """Stopped (by us) → unavailable → Stopped must keep _stopped_by_us and the recovery timer."""
    ctrl, hass, _ = make_controller(charging_states(-500, "Stopped"))
    ctrl._stopped_by_us = True

    ctrl._handle_charger_status_change(status_event("Charging", "Stopped"))
    assert ctrl._unsub_recovery_timer is not None

    ctrl._handle_charger_status_change(status_event("Stopped", "unavailable"))
    assert ctrl._stopped_by_us is True
    assert ctrl._unsub_recovery_timer is not None

    ctrl._handle_charger_status_change(status_event("unavailable", "Stopped"))
    assert ctrl._stopped_by_us is True
    assert ctrl._unsub_recovery_timer is not None


@pytest.mark.asyncio
async def test_unknown_blip_while_charging_keeps_timer():
    """Charging → unknown must not stop the recalculation timer."""
    ctrl, hass, _ = make_controller(charging_states(-3000))
    ctrl._handle_charger_status_change(status_event("Stopped", "Charging"))
    await drain()
    assert ctrl._unsub_timer is not None

    ctrl._handle_charger_status_change(status_event("Charging", "unknown"))
    assert ctrl._unsub_timer is not None
    assert ctrl._is_charging is True


@pytest.mark.asyncio
async def test_blip_back_to_same_status_keeps_stop_delay_running():
    """Charging → unavailable → Charging must not restart stop_delay_s or force a write."""
    ctrl, hass, _ = make_controller(charging_states(200), stop_delay_s=120)
    clock = use_clock(ctrl)
    ctrl._handle_charger_status_change(status_event("Stopped", "Charging"))
    await drain()
    await ctrl._compute_and_apply("timer")          # low surplus starts the countdown
    since = ctrl._low_surplus_since
    writes = hass.services.count("set_value")
    assert since is not None

    clock[0] += 30
    ctrl._handle_charger_status_change(status_event("Charging", "unavailable"))
    ctrl._handle_charger_status_change(status_event("unavailable", "Charging"))
    await drain()

    assert ctrl._low_surplus_since == since
    assert hass.services.count("set_value") == writes


@pytest.mark.asyncio
async def test_blip_to_different_status_is_a_real_transition():
    """Charging → unavailable → Finished is handled (timers stopped)."""
    ctrl, hass, _ = make_controller(charging_states(-3000))
    ctrl._handle_charger_status_change(status_event("Stopped", "Charging"))
    await drain()

    ctrl._handle_charger_status_change(status_event("Charging", "unavailable"))
    ctrl._handle_charger_status_change(status_event("unavailable", "Finished"))

    assert ctrl._is_charging is False
    assert ctrl._unsub_timer is None


@pytest.mark.asyncio
async def test_startup_check_skips_state_already_handled_by_listener():
    """unavailable → Charging during the startup delay must not be followed by a second write."""
    ctrl, hass, _ = make_controller(charging_states(-3000))
    ctrl._handle_charger_status_change(status_event("unavailable", "Charging"))
    await drain()
    writes = hass.services.count("set_value")

    with mock.patch("asyncio.sleep", return_value=None):
        await ctrl._delayed_startup_check()

    assert hass.services.count("set_value") == writes


@pytest.mark.asyncio
async def test_recovery_tick_waits_while_status_unavailable():
    """Recovery tick with unavailable status keeps waiting instead of giving up."""
    ctrl, hass, _ = make_controller(charging_states(-3000, "unavailable"))
    ctrl._stopped_by_us = True
    ctrl._unsub_recovery_timer = MagicMock()

    await ctrl._handle_recovery_timer(None)

    assert ctrl._stopped_by_us is True
    assert ctrl._unsub_recovery_timer is not None
    assert hass.services.count("press") == 0


# ---------------------------------------------------------------------------
# #2 – nothing runs after stop / reload
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_async_stop_cancels_pending_startup_check():
    ctrl, hass, _ = make_controller(charging_states(-3000))
    await ctrl.async_start()
    task = ctrl._startup_task
    assert task is not None

    await ctrl.async_stop()
    await asyncio.gather(task, return_exceptions=True)

    assert task.cancelled()
    assert ctrl._unsub_timer is None


@pytest.mark.asyncio
async def test_startup_check_after_stop_starts_nothing():
    """A startup check that wakes up after the controller stopped must not start a timer."""
    ctrl, hass, _ = make_controller(charging_states(-3000))
    await ctrl.async_stop()

    with mock.patch("asyncio.sleep", return_value=None):
        await ctrl._delayed_startup_check()

    assert ctrl._unsub_timer is None
    assert hass.services.calls == []


@pytest.mark.asyncio
async def test_compute_after_stop_does_nothing():
    ctrl, hass, _ = make_controller(charging_states(-3000))
    ctrl._is_charging = True
    await ctrl.async_stop()

    await ctrl._compute_and_apply("timer")

    assert hass.services.calls == []


# ---------------------------------------------------------------------------
# #3 / #4 – stop delay
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_only_after_stop_delay_and_holds_min_current_meanwhile():
    # Importing 200 W: even with the 6 A charger load added back (1180 W) it stays below 1380 W
    ctrl, hass, _ = make_controller(charging_states(200), stop_delay_s=120)
    clock = use_clock(ctrl)
    ctrl._is_charging = True

    await ctrl._compute_and_apply("timer")
    assert hass.services.count("press") == 0
    set_calls = [c for c in hass.services.calls if c["service"] == "set_value"]
    assert set_calls[-1]["data"]["value"] == 6.0, "Must hold min_current while waiting"

    clock[0] += 60
    await ctrl._compute_and_apply("timer")
    assert hass.services.count("press") == 0

    clock[0] += 60
    await ctrl._compute_and_apply("timer")
    assert hass.services.count("press") == 1
    assert ctrl._stopped_by_us is True


@pytest.mark.asyncio
async def test_short_dip_resets_stop_delay():
    states = charging_states(200)
    states["sensor.charger_power"] = 1380   # measured load → 200 W import = 1180 W available
    ctrl, hass, _ = make_controller(states, stop_delay_s=120, charger_power_entity="sensor.charger_power")
    clock = use_clock(ctrl)
    ctrl._is_charging = True

    await ctrl._compute_and_apply("timer")          # low, t=0
    set_power(ctrl, hass, -3000)
    clock[0] += 60
    await ctrl._compute_and_apply("timer")          # surplus back → delay reset
    set_power(ctrl, hass, 200)
    clock[0] += 60
    await ctrl._compute_and_apply("timer")          # low again, new delay starts
    assert hass.services.count("press") == 0

    clock[0] += 120
    await ctrl._compute_and_apply("timer")
    assert hass.services.count("press") == 1


@pytest.mark.asyncio
async def test_first_tick_after_start_does_not_stop_with_delay():
    """#3: right after charging_started the charger load is unknown (estimated as 0 W).

    With stop_delay_s the underestimated first tick only holds min_current; the next
    tick (estimate known) sees the real surplus and charging continues.
    """
    # Car already draws 6 A (1380 W) → meter shows only 300 W export
    ctrl, hass, _ = make_controller(charging_states(-300), stop_delay_s=120)
    clock = use_clock(ctrl)
    ctrl._is_charging = True

    await ctrl._compute_and_apply("charging_started")
    assert hass.services.count("press") == 0

    clock[0] += 60
    await ctrl._compute_and_apply("timer")  # 300 + 6 A × 230 V = 1680 W ≥ 1380 W
    assert hass.services.count("press") == 0
    assert ctrl._low_surplus_since is None


# ---------------------------------------------------------------------------
# #4 – restart hysteresis and start delay
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restart_requires_hysteresis():
    states = charging_states(-1500, "Stopped")   # 1500 W < 1380 + 200
    ctrl, hass, _ = make_controller(states, start_hysteresis_w=200)
    clock = use_clock(ctrl)
    ctrl._stopped_by_us = True
    ctrl._unsub_recovery_timer = MagicMock()

    await ctrl._handle_recovery_timer(None)
    assert hass.services.count("press") == 0

    set_power(ctrl, hass, -1600)   # 1600 W ≥ 1580 W for the whole next interval
    clock[0] += 60
    await ctrl._handle_recovery_timer(None)
    assert hass.services.count("press") == 1


@pytest.mark.asyncio
async def test_restart_requires_sustained_surplus():
    states = charging_states(-2000, "Stopped")
    ctrl, hass, _ = make_controller(states, start_delay_s=120)
    clock = use_clock(ctrl)
    ctrl._stopped_by_us = True
    ctrl._unsub_recovery_timer = MagicMock()

    await ctrl._handle_recovery_timer(None)          # t=0
    set_power(ctrl, hass, -500)
    clock[0] += 60
    await ctrl._handle_recovery_timer(None)          # dip → reset
    set_power(ctrl, hass, -2000)
    clock[0] += 60
    await ctrl._handle_recovery_timer(None)          # high again, new delay
    clock[0] += 60
    await ctrl._handle_recovery_timer(None)          # only 60 s sustained
    assert hass.services.count("press") == 0

    clock[0] += 60
    await ctrl._handle_recovery_timer(None)          # 120 s sustained
    assert hass.services.count("press") == 1


# ---------------------------------------------------------------------------
# #5 / #6 – button retry policy
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_press_retried_after_cooldown_then_gives_up():
    ctrl, hass, _ = make_controller(charging_states(-3000, "Stopped"))
    clock = use_clock(ctrl)
    ctrl._stopped_by_us = True
    ctrl._unsub_recovery_timer = MagicMock()

    await ctrl._handle_recovery_timer(None)          # attempt 1
    assert ctrl._unsub_recovery_timer is not None, "Recovery must keep running until charger confirms"
    clock[0] += 60
    await ctrl._handle_recovery_timer(None)          # within cooldown
    assert hass.services.count("press") == 1

    clock[0] += 240
    await ctrl._handle_recovery_timer(None)          # attempt 2 (300 s after first)
    clock[0] += 300
    await ctrl._handle_recovery_timer(None)          # attempt 3
    assert hass.services.count("press") == 3

    clock[0] += 300
    await ctrl._handle_recovery_timer(None)          # give up
    assert hass.services.count("press") == 3
    assert ctrl._stopped_by_us is False
    assert ctrl._unsub_recovery_timer is None


@pytest.mark.asyncio
async def test_failed_stop_press_does_not_set_flag_and_retries_after_cooldown():
    ctrl, hass, _ = make_controller(charging_states(200))
    clock = use_clock(ctrl)
    hass.services.failing.add("press")
    ctrl._is_charging = True

    await ctrl._compute_and_apply("timer")
    assert hass.services.count("press") == 1
    assert ctrl._stopped_by_us is False, "Flag must only be set after a successful press"

    clock[0] += 60
    await ctrl._compute_and_apply("timer")
    assert hass.services.count("press") == 1, "No retry within cooldown"

    clock[0] += 240
    await ctrl._compute_and_apply("timer")
    assert hass.services.count("press") == 2


@pytest.mark.asyncio
async def test_stop_gives_up_after_max_attempts_and_holds_min_current():
    states = charging_states(200)
    states["sensor.charger_power"] = 1380   # measured load → 1180 W available every tick
    ctrl, hass, _ = make_controller(states, charger_power_entity="sensor.charger_power")
    clock = use_clock(ctrl)
    ctrl._is_charging = True
    ctrl._last_set_current = 10

    for _ in range(5):
        await ctrl._compute_and_apply("timer")
        clock[0] += 300

    assert hass.services.count("press") == 3
    assert ctrl._last_set_current == 6


@pytest.mark.asyncio
async def test_ineffective_stop_press_cleared_when_surplus_returns():
    states = charging_states(-500)
    ctrl, hass, _ = make_controller(states)
    clock = use_clock(ctrl)
    ctrl._is_charging = True

    await ctrl._compute_and_apply("timer")           # press stop, charger keeps charging
    assert ctrl._stopped_by_us is True

    set_power(ctrl, hass, -3000)
    clock[0] += 60
    await ctrl._compute_and_apply("timer")           # stop may still be in flight
    assert ctrl._stopped_by_us is True

    clock[0] += 240
    await ctrl._compute_and_apply("timer")           # cooldown elapsed, still charging
    assert ctrl._stopped_by_us is False


# ---------------------------------------------------------------------------
# #7 – no status entity
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_button_ignored_without_status_entity():
    ctrl, hass, _ = make_controller(charging_states(-500), charger_status_entity=None)
    assert ctrl.charger_start_stop_button is None
    ctrl._is_charging = True

    await ctrl._compute_and_apply("timer")

    assert hass.services.count("press") == 0
    set_calls = [c for c in hass.services.calls if c["service"] == "set_value"]
    assert set_calls[-1]["data"]["value"] == 6.0


# ---------------------------------------------------------------------------
# #8 – override bypasses min_delta_amp
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["override_value", "override_toggle", "manual_trigger"])
async def test_override_actions_bypass_min_delta(reason):
    ctrl, hass, _ = make_controller(charging_states(-3000), min_delta_amp=2)
    ctrl._override_enabled = True
    ctrl._override_current = 7
    ctrl._last_set_current = 6

    await ctrl._compute_and_apply(reason)

    set_calls = [c for c in hass.services.calls if c["service"] == "set_value"]
    assert len(set_calls) == 1
    assert set_calls[0]["data"]["value"] == 7.0


@pytest.mark.asyncio
async def test_override_timer_tick_still_respects_min_delta():
    ctrl, hass, _ = make_controller(charging_states(-3000), min_delta_amp=2)
    ctrl._override_enabled = True
    ctrl._override_current = 7
    ctrl._last_set_current = 6

    await ctrl._compute_and_apply("timer")

    assert hass.services.count("set_value") == 0


# ---------------------------------------------------------------------------
# #9 – override restarts a charger we stopped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enabling_override_restarts_charger_we_stopped():
    ctrl, hass, _ = make_controller(charging_states(-500, "Stopped"))
    ctrl._stopped_by_us = True
    ctrl._unsub_recovery_timer = MagicMock()

    ctrl.set_override(True)
    await drain()

    assert hass.services.count("press") == 1
    assert ctrl._stopped_by_us is False
    assert ctrl._unsub_recovery_timer is None


@pytest.mark.asyncio
async def test_enabling_override_does_not_toggle_when_charger_not_stopped():
    """Stop pressed but charger still reports Charging – pressing again would toggle it."""
    ctrl, hass, _ = make_controller(charging_states(-500, "Charging"))
    ctrl._stopped_by_us = True

    ctrl.set_override(True)
    await drain()

    assert hass.services.count("press") == 0


@pytest.mark.asyncio
async def test_disabling_stop_switch_does_not_toggle_when_charger_not_stopped():
    ctrl, hass, _ = make_controller(charging_states(-500, "Charging"))
    ctrl._stopped_by_us = True

    ctrl.set_stop_on_no_injection(False)
    await drain()

    assert hass.services.count("press") == 0
    assert ctrl._stopped_by_us is False


# ---------------------------------------------------------------------------
# Computed-current sensor and voltage fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("stopped_by_us,new_status", [(True, "Stopped"), (False, "Finished")])
async def test_computed_current_zero_when_charging_stops(stopped_by_us, new_status):
    ctrl, hass, _ = make_controller(charging_states(-3000))
    ctrl._computed_current = 13
    ctrl._stopped_by_us = stopped_by_us

    ctrl._handle_charger_status_change(status_event("Charging", new_status))

    assert ctrl.get_computed_current() == 0


@pytest.mark.asyncio
async def test_recovery_uses_230v_fallback_for_non_numeric_voltage():
    states = charging_states(-1500, "Stopped")
    states["sensor.grid_voltage"] = "garbage"
    ctrl, hass, _ = make_controller(states)
    ctrl._stopped_by_us = True
    ctrl._unsub_recovery_timer = MagicMock()

    await ctrl._handle_recovery_timer(None)   # 1500 W ≥ 6 × 230 = 1380 W

    assert hass.services.count("press") == 1


@pytest.mark.asyncio
async def test_recovery_skips_when_voltage_unavailable():
    states = charging_states(-3000, "Stopped")
    states["sensor.grid_voltage"] = "unavailable"
    ctrl, hass, _ = make_controller(states)
    ctrl._stopped_by_us = True
    ctrl._unsub_recovery_timer = MagicMock()

    await ctrl._handle_recovery_timer(None)

    assert hass.services.count("press") == 0
