"""Unit tests for the minimum-surplus threshold logic (v1.2.0 FR).

These tests exercise EVSolarController._compute_and_apply() and
_handle_recovery_timer() in isolation, using a lightweight fake HomeAssistant
stub – no real HA instance is needed.

Scenario under test
-------------------
The controller must stop the charger (press the start/stop button) whenever:

    available_w < min_current × voltage × phases

i.e. even when the grid meter shows *some* solar injection, if that injection
is less than the minimum viable charging current in watts the charger would
draw the remainder from the grid.  The recovery timer must restart charging
only once the surplus reaches or exceeds this threshold.

Run with:
    python -m pytest tests/test_min_surplus_threshold.py -v
"""

from __future__ import annotations

import asyncio
import types
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Minimal HA stubs
# ---------------------------------------------------------------------------

class FakeState:
    def __init__(self, state: str):
        self.state = state


class FakeStates:
    def __init__(self, mapping: dict):
        self._map = mapping

    def get(self, entity_id: str):
        val = self._map.get(entity_id)
        if val is None:
            return None
        return FakeState(str(val))


class FakeServices:
    """Records service calls made during the test."""

    def __init__(self):
        self.calls: list[dict] = []

    async def async_call(self, domain, service, data=None, blocking=False):
        self.calls.append({"domain": domain, "service": service, "data": data or {}})


class FakeHass:
    def __init__(self, states_map: dict):
        self.states = FakeStates(states_map)
        self.services = FakeServices()

    def async_create_task(self, coro):
        """Schedule coroutine – in tests we run it synchronously via asyncio."""
        loop = asyncio.get_event_loop()
        return loop.create_task(coro)


# ---------------------------------------------------------------------------
# Helper: build a controller with typical defaults
# ---------------------------------------------------------------------------

def make_controller(
    states: dict,
    *,
    min_current: int = 6,
    phases: int = 1,
    stop_on_no_injection: bool = True,
    charger_start_stop_button: str = "button.charger_toggle",
    charger_status_entity: str = "sensor.charger_status",
    charging_state: str = "Charging",
    stopped_state: str = "Stopped",
    export_is_negative: bool = True,
    safety_margin_w: float = 0.0,
):
    """Import EVSolarController after patching HA imports."""
    # We need to import the module; patch HA symbols first.
    import sys
    import importlib

    # Build thin HA module stubs so the import doesn't fail outside HA
    ha_stubs = {
        "homeassistant": types.ModuleType("homeassistant"),
        "homeassistant.core": types.ModuleType("homeassistant.core"),
        "homeassistant.const": types.ModuleType("homeassistant.const"),
        "homeassistant.helpers": types.ModuleType("homeassistant.helpers"),
        "homeassistant.helpers.event": types.ModuleType("homeassistant.helpers.event"),
        "homeassistant.helpers.typing": types.ModuleType("homeassistant.helpers.typing"),
        "homeassistant.helpers.discovery": types.ModuleType("homeassistant.helpers.discovery"),
        "homeassistant.helpers.device_registry": types.ModuleType("homeassistant.helpers.device_registry"),
        "homeassistant.config_entries": types.ModuleType("homeassistant.config_entries"),
        "homeassistant.components": types.ModuleType("homeassistant.components"),
        "homeassistant.components.switch": types.ModuleType("homeassistant.components.switch"),
        "homeassistant.components.number": types.ModuleType("homeassistant.components.number"),
        "homeassistant.components.sensor": types.ModuleType("homeassistant.components.sensor"),
        "homeassistant.components.button": types.ModuleType("homeassistant.components.button"),
        "homeassistant.helpers.entity_platform": types.ModuleType("homeassistant.helpers.entity_platform"),
    }

    # Minimal symbols needed by the module
    ha_stubs["homeassistant.core"].HomeAssistant = object
    ha_stubs["homeassistant.core"].callback = lambda f: f
    ha_stubs["homeassistant.const"].EVENT_HOMEASSISTANT_STOP = "homeassistant_stop"
    ha_stubs["homeassistant.helpers.event"].async_track_time_interval = MagicMock(return_value=MagicMock())
    ha_stubs["homeassistant.helpers.event"].async_track_state_change_event = MagicMock(return_value=MagicMock())
    ha_stubs["homeassistant.helpers.typing"].ConfigType = dict
    ha_stubs["homeassistant.config_entries"].ConfigEntry = object
    ha_stubs["homeassistant.helpers.device_registry"].DeviceInfo = dict

    # Patch sys.modules for the duration of this import
    original = {}
    for k, v in ha_stubs.items():
        original[k] = sys.modules.get(k)
        sys.modules[k] = v

    # Force re-import so our stubs take effect (or first import)
    pkg = "custom_components.ev_solar_manager"
    init_mod = f"{pkg}.__init__"
    for key in list(sys.modules.keys()):
        if key.startswith(pkg):
            del sys.modules[key]

    import importlib.util, os
    spec = importlib.util.spec_from_file_location(
        init_mod,
        os.path.join(os.path.dirname(__file__), "..", "custom_components", "ev_solar_manager", "__init__.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[init_mod] = mod
    # Provide const sub-module inline
    const_spec = importlib.util.spec_from_file_location(
        f"{pkg}.const",
        os.path.join(os.path.dirname(__file__), "..", "custom_components", "ev_solar_manager", "const.py"),
    )
    const_mod = importlib.util.module_from_spec(const_spec)
    sys.modules[f"{pkg}.const"] = const_mod
    const_spec.loader.exec_module(const_mod)
    # Provide a stub device module
    device_mod = types.ModuleType(f"{pkg}.device")
    device_mod.ev_solar_device_info = lambda: {}
    sys.modules[f"{pkg}.device"] = device_mod

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
        export_is_negative=export_is_negative,
        phases=phases,
        safety_margin_w=safety_margin_w,
        charger_status_entity=charger_status_entity,
        charging_state=charging_state,
        charger_start_stop_button=charger_start_stop_button,
        stopped_state=stopped_state,
    )
    ctrl._is_charging = True
    ctrl._stop_on_no_injection = stop_on_no_injection
    return ctrl, hass


# ---------------------------------------------------------------------------
# Tests: _compute_and_apply threshold behaviour
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_when_surplus_below_min_threshold():
    """available_w < min_current×V×phases → button pressed, _stopped_by_us=True."""
    # 230 V × 6 A × 1 phase = 1380 W threshold
    # Export = 500 W (positive solar but < 1380 W) → should stop
    states = {
        "sensor.grid_power": -500,   # export_is_negative=True → export = +500 W
        "sensor.grid_voltage": 230,
    }
    ctrl, hass = make_controller(states)

    await ctrl._compute_and_apply("timer")

    button_calls = [c for c in hass.services.calls if c["service"] == "press"]
    assert len(button_calls) == 1, "Expected one button press (stop)"
    assert ctrl._stopped_by_us is True


@pytest.mark.asyncio
async def test_no_stop_when_surplus_exactly_at_threshold():
    """available_w == min_current×V×phases → charger runs at min_current, no stop."""
    # 230 × 6 × 1 = 1380 W exactly
    states = {
        "sensor.grid_power": -1380,
        "sensor.grid_voltage": 230,
    }
    ctrl, hass = make_controller(states)

    await ctrl._compute_and_apply("timer")

    button_calls = [c for c in hass.services.calls if c["service"] == "press"]
    assert len(button_calls) == 0, "No stop expected when surplus == threshold"
    number_calls = [c for c in hass.services.calls if c["service"] == "set_value"]
    assert len(number_calls) == 1
    assert number_calls[0]["data"]["value"] == 6.0


@pytest.mark.asyncio
async def test_no_stop_when_surplus_above_threshold():
    """available_w >> threshold → charger is set to a higher current, no stop."""
    # 2300 W available → 10 A
    states = {
        "sensor.grid_power": -2300,
        "sensor.grid_voltage": 230,
    }
    ctrl, hass = make_controller(states)

    await ctrl._compute_and_apply("timer")

    button_calls = [c for c in hass.services.calls if c["service"] == "press"]
    assert len(button_calls) == 0
    number_calls = [c for c in hass.services.calls if c["service"] == "set_value"]
    assert len(number_calls) == 1
    assert number_calls[0]["data"]["value"] == 10.0


@pytest.mark.asyncio
async def test_stop_when_drawing_from_grid():
    """available_w < 0 (importing from grid) → button pressed."""
    states = {
        "sensor.grid_power": 200,   # importing 200 W from grid
        "sensor.grid_voltage": 230,
    }
    ctrl, hass = make_controller(states)

    await ctrl._compute_and_apply("timer")

    button_calls = [c for c in hass.services.calls if c["service"] == "press"]
    assert len(button_calls) == 1
    assert ctrl._stopped_by_us is True


@pytest.mark.asyncio
async def test_already_stopped_does_not_double_press():
    """If _stopped_by_us is already True, the button is NOT pressed again."""
    states = {
        "sensor.grid_power": -100,  # only 100 W surplus, well below 1380 W threshold
        "sensor.grid_voltage": 230,
    }
    ctrl, hass = make_controller(states)
    ctrl._stopped_by_us = True   # already stopped by us

    await ctrl._compute_and_apply("timer")

    button_calls = [c for c in hass.services.calls if c["service"] == "press"]
    assert len(button_calls) == 0, "Button must not be pressed twice"


@pytest.mark.asyncio
async def test_three_phase_threshold():
    """Three-phase: threshold = min_current × voltage × 3."""
    # threshold = 6 × 230 × 3 = 4140 W
    # surplus = 3000 W → below threshold → stop
    states = {
        "sensor.grid_power": -3000,
        "sensor.grid_voltage": 230,
    }
    ctrl, hass = make_controller(states, phases=3)

    await ctrl._compute_and_apply("timer")

    button_calls = [c for c in hass.services.calls if c["service"] == "press"]
    assert len(button_calls) == 1, "3-phase: 3000 W < 4140 W threshold → should stop"


@pytest.mark.asyncio
async def test_three_phase_above_threshold():
    """Three-phase: surplus 5000 W > 4140 W threshold → no stop, correct current set."""
    # 5000 W / (230 × 3) = 7.25 A → round to 7 A → clamped to [6, 24] → 7 A
    states = {
        "sensor.grid_power": -5000,
        "sensor.grid_voltage": 230,
    }
    ctrl, hass = make_controller(states, phases=3)

    await ctrl._compute_and_apply("timer")

    button_calls = [c for c in hass.services.calls if c["service"] == "press"]
    assert len(button_calls) == 0
    number_calls = [c for c in hass.services.calls if c["service"] == "set_value"]
    assert len(number_calls) == 1
    assert number_calls[0]["data"]["value"] == 7.0


@pytest.mark.asyncio
async def test_stop_on_no_injection_disabled_keeps_min_current():
    """When stop_on_no_injection=False, controller falls back to min_current even below threshold."""
    states = {
        "sensor.grid_power": -500,
        "sensor.grid_voltage": 230,
    }
    ctrl, hass = make_controller(states, stop_on_no_injection=False)

    await ctrl._compute_and_apply("timer")

    button_calls = [c for c in hass.services.calls if c["service"] == "press"]
    assert len(button_calls) == 0, "No stop expected when stop_on_no_injection=False"
    number_calls = [c for c in hass.services.calls if c["service"] == "set_value"]
    assert len(number_calls) == 1
    assert number_calls[0]["data"]["value"] == 6.0  # min_current fallback


# ---------------------------------------------------------------------------
# Tests: _handle_recovery_timer threshold behaviour
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recovery_timer_restarts_when_surplus_sufficient():
    """Recovery timer presses start when surplus >= min_current×V×phases."""
    # 1500 W > 1380 W → restart
    states = {
        "sensor.grid_power": -1500,
        "sensor.grid_voltage": 230,
        "sensor.charger_status": "Stopped",
    }
    ctrl, hass = make_controller(states)
    ctrl._stopped_by_us = True
    # Stub the recovery timer unsubscribe
    ctrl._unsub_recovery_timer = MagicMock()

    await ctrl._handle_recovery_timer(None)

    button_calls = [c for c in hass.services.calls if c["service"] == "press"]
    assert len(button_calls) == 1, "Start button should be pressed when surplus returns"


@pytest.mark.asyncio
async def test_recovery_timer_waits_when_surplus_still_low():
    """Recovery timer does NOT press start when surplus < threshold."""
    # 800 W < 1380 W → keep waiting
    states = {
        "sensor.grid_power": -800,
        "sensor.grid_voltage": 230,
        "sensor.charger_status": "Stopped",
    }
    ctrl, hass = make_controller(states)
    ctrl._stopped_by_us = True
    ctrl._unsub_recovery_timer = MagicMock()

    await ctrl._handle_recovery_timer(None)

    button_calls = [c for c in hass.services.calls if c["service"] == "press"]
    assert len(button_calls) == 0, "Start button must not be pressed while surplus is still below threshold"


@pytest.mark.asyncio
async def test_recovery_timer_waits_when_still_importing():
    """Recovery timer does NOT press start when grid is being imported."""
    states = {
        "sensor.grid_power": 300,   # importing 300 W
        "sensor.grid_voltage": 230,
        "sensor.charger_status": "Stopped",
    }
    ctrl, hass = make_controller(states)
    ctrl._stopped_by_us = True
    ctrl._unsub_recovery_timer = MagicMock()

    await ctrl._handle_recovery_timer(None)

    button_calls = [c for c in hass.services.calls if c["service"] == "press"]
    assert len(button_calls) == 0

