"""Tests for startup recovery behaviour (v1.2.1 fix).

Scenario
--------
When HA restarts while the EV charger is in stopped_state, the controller must
resume charging only if WE had stopped it (lack of surplus). _stopped_by_us is
persisted in HA storage and restored in async_start(); a stopped_state caused by
the user or by a full car (often the same state string, e.g. "Finished") must
not trigger an automatic start.

_delayed_startup_check() arms the recovery timer when the charger is in
stopped_state AND the restored _stopped_by_us flag is True.

Run with:
    python -m pytest tests/test_startup_recovery.py -v
"""

from __future__ import annotations

import unittest.mock as mock
import pytest
from unittest.mock import MagicMock

from tests.conftest import make_controller


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_startup_arms_recovery_when_charger_stopped():
    """After HA restart with charger stopped by us (restored flag), recovery timer is armed."""
    states = {
        "sensor.charger_status": "Stopped",
        "sensor.grid_power": -2000,
        "sensor.grid_voltage": 230,
    }
    ctrl, hass, _ = make_controller(states)
    ctrl._stopped_by_us = True   # restored from storage: we had stopped it before the restart

    with mock.patch("asyncio.sleep", return_value=None):
        await ctrl._delayed_startup_check()

    assert ctrl._stopped_by_us is True, "_stopped_by_us must be True after startup with stopped charger"
    assert ctrl._unsub_recovery_timer is not None, "Recovery timer must be armed"


@pytest.mark.asyncio
async def test_startup_does_not_arm_recovery_when_switch_off():
    """If stop_on_no_injection=False, recovery timer is NOT armed at startup."""
    states = {"sensor.charger_status": "Stopped"}
    ctrl, hass, _ = make_controller(states, stop_on_no_injection=False)

    with mock.patch("asyncio.sleep", return_value=None):
        await ctrl._delayed_startup_check()

    assert ctrl._stopped_by_us is False
    assert ctrl._unsub_recovery_timer is None, "Recovery timer must NOT be armed when switch is OFF"


@pytest.mark.asyncio
async def test_startup_does_not_arm_recovery_without_button():
    """If charger_start_stop_button is not configured, recovery timer is NOT armed."""
    states = {"sensor.charger_status": "Stopped"}
    ctrl, hass, _ = make_controller(states, charger_start_stop_button=None)

    with mock.patch("asyncio.sleep", return_value=None):
        await ctrl._delayed_startup_check()

    assert ctrl._stopped_by_us is False
    assert ctrl._unsub_recovery_timer is None


@pytest.mark.asyncio
async def test_startup_starts_timer_when_already_charging():
    """If charger is already in charging_state at startup, normal timer starts."""
    states = {
        "sensor.charger_status": "Charging",
        "sensor.grid_power": -2000,
        "sensor.grid_voltage": 230,
    }
    ctrl, hass, _ = make_controller(states)

    with mock.patch("asyncio.sleep", return_value=None):
        await ctrl._delayed_startup_check()

    assert ctrl._is_charging is True
    assert ctrl._unsub_timer is not None, "Recalc timer must start when charger is Charging at startup"
    assert ctrl._unsub_recovery_timer is None


@pytest.mark.asyncio
async def test_startup_stays_idle_when_disconnected():
    """If charger is disconnected/finished at startup, nothing is armed."""
    states = {"sensor.charger_status": "Status.Finished"}
    ctrl, hass, _ = make_controller(states, stopped_state="Stopped")  # Finished != Stopped

    with mock.patch("asyncio.sleep", return_value=None):
        await ctrl._delayed_startup_check()

    assert ctrl._is_charging is False
    assert ctrl._stopped_by_us is False
    assert ctrl._unsub_timer is None
    assert ctrl._unsub_recovery_timer is None


@pytest.mark.asyncio
async def test_full_restart_flow_charger_restarts_with_surplus():
    """Full flow: startup with stopped charger + sufficient surplus → start button pressed."""
    states = {
        "sensor.charger_status": "Stopped",
        "sensor.grid_power": -2000,   # 2000 W export > 1380 W threshold
        "sensor.grid_voltage": 230,
    }
    ctrl, hass, _ = make_controller(states)
    ctrl._stopped_by_us = True   # restored from storage: we had stopped it before the restart

    with mock.patch("asyncio.sleep", return_value=None):
        await ctrl._delayed_startup_check()

    ctrl._unsub_recovery_timer = MagicMock()
    await ctrl._handle_recovery_timer(None)

    button_calls = [c for c in hass.services.calls if c["service"] == "press"]
    assert len(button_calls) == 1, "Start button should be pressed when surplus is sufficient"


@pytest.mark.asyncio
async def test_full_restart_flow_charger_waits_without_surplus():
    """Full flow: startup with stopped charger + insufficient surplus → waits."""
    states = {
        "sensor.charger_status": "Stopped",
        "sensor.grid_power": -500,   # 500 W < 1380 W threshold
        "sensor.grid_voltage": 230,
    }
    ctrl, hass, _ = make_controller(states)

    with mock.patch("asyncio.sleep", return_value=None):
        await ctrl._delayed_startup_check()

    ctrl._unsub_recovery_timer = MagicMock()
    await ctrl._handle_recovery_timer(None)

    button_calls = [c for c in hass.services.calls if c["service"] == "press"]
    assert len(button_calls) == 0, "Start button must NOT be pressed when surplus is still too low"


@pytest.mark.asyncio
async def test_startup_stays_idle_when_stopped_not_by_us():
    """Charger in stopped_state but not stopped by us (car full / manual stop) → no recovery."""
    states = {
        "sensor.charger_status": "Stopped",
        "sensor.grid_power": -3000,
        "sensor.grid_voltage": 230,
    }
    ctrl, hass, _ = make_controller(states)

    with mock.patch("asyncio.sleep", return_value=None):
        await ctrl._delayed_startup_check()

    assert ctrl._stopped_by_us is False
    assert ctrl._unsub_recovery_timer is None


@pytest.mark.asyncio
async def test_stopped_by_us_is_persisted_and_restored():
    states = {
        "sensor.charger_status": "Stopped",
        "sensor.grid_power": -3000,
        "sensor.grid_voltage": 230,
    }
    ctrl, hass, _ = make_controller(states)
    ctrl._stopped_by_us = True
    saved = ctrl._store.data
    assert saved == {"stopped_by_us": True}

    # Simulate a restart: new controller, same storage content
    ctrl2, _, _ = make_controller(states)
    ctrl2._store.data = saved
    await ctrl2.async_start()
    assert ctrl2._stopped_by_us is True
    await ctrl2.async_stop()


@pytest.mark.asyncio
async def test_startup_clears_stale_flag_when_not_stopped():
    """Restored flag but the charger is now Finished/disconnected → flag cleared, idle."""
    states = {"sensor.charger_status": "Disconnected"}
    ctrl, hass, _ = make_controller(states)
    ctrl._stopped_by_us = True

    with mock.patch("asyncio.sleep", return_value=None):
        await ctrl._delayed_startup_check()

    assert ctrl._stopped_by_us is False
    assert ctrl._store.data == {"stopped_by_us": False}
