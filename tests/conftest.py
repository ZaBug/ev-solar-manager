"""Shared fixtures and stubs for EV Solar Manager tests."""

from __future__ import annotations

import asyncio
import os
import sys
import types
import importlib.util
from unittest.mock import MagicMock


class FakeState:
    def __init__(self, state: str):
        self.state = state


class FakeStates:
    def __init__(self, mapping: dict):
        self._map = mapping

    def get(self, entity_id: str):
        val = self._map.get(entity_id)
        return FakeState(str(val)) if val is not None else None

    def set(self, entity_id: str, value) -> None:
        self._map[entity_id] = value


class FakeServices:
    def __init__(self, states: "FakeStates | None" = None):
        self._states = states
        self.calls: list[dict] = []
        self.failing: set[str] = set()   # service names that raise (e.g. {"press"})
        self.on_call = None              # optional hook(domain, service, data) run during the call

    async def async_call(self, domain, service, data=None, blocking=False):
        self.calls.append({"domain": domain, "service": service, "data": data or {}})
        if self.on_call is not None:
            self.on_call(domain, service, data or {})
        if service in self.failing:
            raise RuntimeError(f"{domain}.{service} failed")
        if domain == "number" and service == "set_value" and self._states is not None:
            self._states.set(data["entity_id"], data["value"])   # like HA: entity reflects the write

    def count(self, service: str) -> int:
        return sum(1 for c in self.calls if c["service"] == service)


class FakeStore:
    """Stub for homeassistant.helpers.storage.Store – keeps data in memory."""

    def __init__(self, hass, version, key):
        self.data: dict | None = None
        self.saves: int = 0

    async def async_load(self):
        return self.data

    def async_delay_save(self, data_func, delay=0):
        self.data = data_func()
        self.saves += 1


class FakeHass:
    def __init__(self, states_map: dict):
        self.states = FakeStates(states_map)
        self.services = FakeServices(self.states)

    def async_create_task(self, coro):
        return asyncio.get_running_loop().create_task(coro)


def make_controller(
    states: dict,
    *,
    min_current: int = 6,
    max_current: int = 24,
    phases: int = 1,
    stop_on_no_injection: bool = True,
    charger_start_stop_button: str | None = "button.charger_toggle",
    charger_status_entity: str | None = "sensor.charger_status",
    charging_state: str = "Charging",
    stopped_state: str = "Stopped",
    export_is_negative: bool = True,
    safety_margin_w: float = 0.0,
    min_delta_amp: int = 1,
    charger_power_entity: str | None = None,
    start_hysteresis_w: float = 0.0,
    stop_delay_s: float = 0.0,
    start_delay_s: float = 0.0,
):
    """Build an EVSolarController with HA module stubs, no real HA instance needed.

    Anti-flapping defaults are 0 here (immediate stop/start) so threshold tests stay
    focused; tests for hysteresis/delays pass explicit values.
    The controller is marked available, as after async_start().

    Returns (controller, FakeHass, loaded_module).
    """
    ha_stubs = {
        "homeassistant": types.ModuleType("homeassistant"),
        "homeassistant.core": types.ModuleType("homeassistant.core"),
        "homeassistant.const": types.ModuleType("homeassistant.const"),
        "homeassistant.helpers": types.ModuleType("homeassistant.helpers"),
        "homeassistant.helpers.event": types.ModuleType("homeassistant.helpers.event"),
        "homeassistant.helpers.typing": types.ModuleType("homeassistant.helpers.typing"),
        "homeassistant.helpers.storage": types.ModuleType("homeassistant.helpers.storage"),
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
    ha_stubs["homeassistant.core"].HomeAssistant = object
    ha_stubs["homeassistant.core"].callback = lambda f: f
    ha_stubs["homeassistant.const"].EVENT_HOMEASSISTANT_STOP = "homeassistant_stop"
    ha_stubs["homeassistant.helpers.event"].async_track_time_interval = MagicMock(return_value=MagicMock())
    ha_stubs["homeassistant.helpers.event"].async_track_state_change_event = MagicMock(return_value=MagicMock())
    ha_stubs["homeassistant.helpers.typing"].ConfigType = dict
    ha_stubs["homeassistant.helpers.storage"].Store = FakeStore
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

    device_mod = types.ModuleType(f"{pkg}.device")
    device_mod.ev_solar_device_info = lambda: {}
    sys.modules[f"{pkg}.device"] = device_mod

    spec = importlib.util.spec_from_file_location(f"{pkg}.__init__", os.path.join(base, "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{pkg}.__init__"] = mod
    spec.loader.exec_module(mod)

    states.setdefault("number.charger_current", min_current)   # target_number must exist
    hass = FakeHass(states)
    ctrl = mod.EVSolarController(
        hass=hass,
        power_entity="sensor.grid_power",
        voltage_entity="sensor.grid_voltage",
        target_number="number.charger_current",
        min_current=min_current,
        max_current=max_current,
        min_delta_amp=min_delta_amp,
        update_interval=60,
        export_is_negative=export_is_negative,
        phases=phases,
        safety_margin_w=safety_margin_w,
        charger_power_entity=charger_power_entity,
        charger_status_entity=charger_status_entity,
        charging_state=charging_state,
        charger_start_stop_button=charger_start_stop_button,
        stopped_state=stopped_state,
        start_hysteresis_w=start_hysteresis_w,
        stop_delay_s=stop_delay_s,
        start_delay_s=start_delay_s,
    )
    ctrl._stop_on_no_injection = stop_on_no_injection
    ctrl._available = True
    return ctrl, hass, mod
