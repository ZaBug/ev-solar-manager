"""Tests for startup recovery behaviour (v1.2.1 fix).

Scenario
--------
When HA restarts while the EV charger is in stopped_state (e.g. we had stopped
it due to low surplus, or the charger is simply waiting), the _stopped_by_us
flag is lost from memory.

Before the fix: the controller stayed idle forever – charging never resumed
automatically; the user had to press the start button manually.

After the fix: _delayed_startup_check() detects stopped_state at startup,
sets _stopped_by_us=True and arms the recovery timer → charging resumes
automatically once solar surplus reaches min_surplus_w.

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
    """After HA restart with charger in stopped_state, recovery timer is armed."""
    states = {
        "sensor.charger_status": "Stopped",
        "sensor.grid_power": -2000,
        "sensor.grid_voltage": 230,
    }
    ctrl, hass, _ = make_controller(states)

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
