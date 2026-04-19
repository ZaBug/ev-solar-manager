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

import asyncio
import types
import pytest
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Re-use the same HA stubs / make_controller from test_min_surplus_threshold
# ---------------------------------------------------------------------------

class FakeState:
    def __init__(self, state: str):
        self.state = state


class FakeStates:
    def __init__(self, mapping: dict):
        self._map = mapping

    def get(self, entity_id: str):
        val = self._map.get(entity_id)
        return FakeState(str(val)) if val is not None else None


class FakeServices:
    def __init__(self):
        self.calls: list[dict] = []

    async def async_call(self, domain, service, data=None, blocking=False):
        self.calls.append({"domain": domain, "service": service, "data": data or {}})


class FakeHass:
    def __init__(self, states_map: dict):
        self.states = FakeStates(states_map)
        self.services = FakeServices()

    def async_create_task(self, coro):
        loop = asyncio.get_event_loop()
        return loop.create_task(coro)


def make_controller(states, *, stop_on_no_injection=True,
                    charger_start_stop_button="button.charger_toggle",
                    stopped_state="Stopped", charging_state="Charging",
                    min_current=6, phases=1):
    import sys, importlib.util, os, types as _types

    ha_stubs = {
        "homeassistant": _types.ModuleType("homeassistant"),
        "homeassistant.core": _types.ModuleType("homeassistant.core"),
        "homeassistant.const": _types.ModuleType("homeassistant.const"),
        "homeassistant.helpers": _types.ModuleType("homeassistant.helpers"),
        "homeassistant.helpers.event": _types.ModuleType("homeassistant.helpers.event"),
        "homeassistant.helpers.typing": _types.ModuleType("homeassistant.helpers.typing"),
        "homeassistant.helpers.discovery": _types.ModuleType("homeassistant.helpers.discovery"),
        "homeassistant.helpers.device_registry": _types.ModuleType("homeassistant.helpers.device_registry"),
        "homeassistant.config_entries": _types.ModuleType("homeassistant.config_entries"),
        "homeassistant.components": _types.ModuleType("homeassistant.components"),
        "homeassistant.components.switch": _types.ModuleType("homeassistant.components.switch"),
        "homeassistant.components.number": _types.ModuleType("homeassistant.components.number"),
        "homeassistant.components.sensor": _types.ModuleType("homeassistant.components.sensor"),
        "homeassistant.components.button": _types.ModuleType("homeassistant.components.button"),
        "homeassistant.helpers.entity_platform": _types.ModuleType("homeassistant.helpers.entity_platform"),
    }
    ha_stubs["homeassistant.core"].HomeAssistant = object
    ha_stubs["homeassistant.core"].callback = lambda f: f
    ha_stubs["homeassistant.const"].EVENT_HOMEASSISTANT_STOP = "homeassistant_stop"
    ha_stubs["homeassistant.helpers.event"].async_track_time_interval = MagicMock(return_value=MagicMock())
    ha_stubs["homeassistant.helpers.event"].async_track_state_change_event = MagicMock(return_value=MagicMock())
    ha_stubs["homeassistant.helpers.typing"].ConfigType = dict
    ha_stubs["homeassistant.config_entries"].ConfigEntry = object
    ha_stubs["homeassistant.helpers.device_registry"].DeviceInfo = dict

    for k, v in ha_stubs.items():
        sys.modules[k] = v

    pkg = "custom_components.ev_solar_manager"
    for key in list(sys.modules.keys()):
        if key.startswith(pkg):
            del sys.modules[key]

    base = os.path.join(os.path.dirname(__file__), "..", "custom_components", "ev_solar_manager")
    const_spec = importlib.util.spec_from_file_location(f"{pkg}.const", os.path.join(base, "const.py"))
    const_mod = importlib.util.module_from_spec(const_spec)
    sys.modules[f"{pkg}.const"] = const_mod
    const_spec.loader.exec_module(const_mod)

    device_mod = _types.ModuleType(f"{pkg}.device")
    device_mod.ev_solar_device_info = lambda: {}
    sys.modules[f"{pkg}.device"] = device_mod

    spec = importlib.util.spec_from_file_location(f"{pkg}.__init__", os.path.join(base, "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{pkg}.__init__"] = mod
    spec.loader.exec_module(mod)

    hass = FakeHass(states)
    ctrl = mod.EVSolarController(
        hass=hass,
        power_entity="sensor.grid_power",
        voltage_entity="sensor.grid_voltage",
        target_number="number.charger_current",
        min_current=min_current,
        max_current=24,
        min_delta_amp=1,
        update_interval=60,
        phases=phases,
        charger_status_entity="sensor.charger_status",
        charging_state=charging_state,
        charger_start_stop_button=charger_start_stop_button,
        stopped_state=stopped_state,
    )
    ctrl._stop_on_no_injection = stop_on_no_injection
    return ctrl, hass, mod


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

    # Patch asyncio.sleep so the test doesn't actually wait 10 s
    import unittest.mock as mock
    with mock.patch("asyncio.sleep", return_value=None):
        await ctrl._delayed_startup_check()

    assert ctrl._stopped_by_us is True, "_stopped_by_us must be True after startup with stopped charger"
    assert ctrl._unsub_recovery_timer is not None, "Recovery timer must be armed"


@pytest.mark.asyncio
async def test_startup_does_not_arm_recovery_when_switch_off():
    """If stop_on_no_injection=False, recovery timer is NOT armed at startup."""
    states = {"sensor.charger_status": "Stopped"}
    ctrl, hass, _ = make_controller(states, stop_on_no_injection=False)

    import unittest.mock as mock
    with mock.patch("asyncio.sleep", return_value=None):
        await ctrl._delayed_startup_check()

    assert ctrl._stopped_by_us is False
    assert ctrl._unsub_recovery_timer is None, "Recovery timer must NOT be armed when switch is OFF"


@pytest.mark.asyncio
async def test_startup_does_not_arm_recovery_without_button():
    """If charger_start_stop_button is not configured, recovery timer is NOT armed."""
    states = {"sensor.charger_status": "Stopped"}
    ctrl, hass, _ = make_controller(states, charger_start_stop_button=None)

    import unittest.mock as mock
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

    import unittest.mock as mock
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

    import unittest.mock as mock
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

    import unittest.mock as mock
    with mock.patch("asyncio.sleep", return_value=None):
        await ctrl._delayed_startup_check()

    # Recovery timer is armed – simulate one tick
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

    import unittest.mock as mock
    with mock.patch("asyncio.sleep", return_value=None):
        await ctrl._delayed_startup_check()

    ctrl._unsub_recovery_timer = MagicMock()
    await ctrl._handle_recovery_timer(None)

    button_calls = [c for c in hass.services.calls if c["service"] == "press"]
    assert len(button_calls) == 0, "Start button must NOT be pressed when surplus is still too low"

