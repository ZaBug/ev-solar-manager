"""Shared fixtures and stubs for EV Solar Manager tests."""

from __future__ import annotations

import asyncio
import os
import sys
import types
import importlib.util
from unittest.mock import MagicMock


class FakeState:
    def __init__(self, state: str, attributes: dict | None = None):
        self.state = state
        self.attributes = attributes or {}


class FakeStates:
    def __init__(self, mapping: dict, attributes: dict | None = None):
        self._map = mapping
        self._attributes = attributes or {}   # entity_id -> attributes dict

    def get(self, entity_id: str):
        val = self._map.get(entity_id)
        return FakeState(str(val), self._attributes.get(entity_id)) if val is not None else None

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


def _load_submodule(pkg: str, base: str, name: str) -> types.ModuleType:
    """Load custom_components/ev_solar_manager/<name>.py as <pkg>.<name> (no package import)."""
    spec = importlib.util.spec_from_file_location(f"{pkg}.{name}", os.path.join(base, f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"{pkg}.{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


class _FakeFlowHandler:
    """Minimal FlowHandler: forms and results are returned as plain dicts."""

    hass = None

    def async_show_form(self, *, step_id, data_schema, errors=None):
        return {"type": "form", "step_id": step_id, "data_schema": data_schema, "errors": errors or {}}

    def add_suggested_values_to_schema(self, data_schema, suggested_values):
        data_schema.suggested = {
            str(key): suggested_values[str(key)] for key in data_schema.schema if str(key) in suggested_values
        }
        return data_schema


class FakeConfigFlow(_FakeFlowHandler):
    def __init_subclass__(cls, domain=None, **kwargs):
        super().__init_subclass__(**kwargs)

    unique_id = None
    configured_ids: set = set()

    async def async_set_unique_id(self, unique_id):
        self.unique_id = unique_id

    def _abort_if_unique_id_configured(self):
        if self.unique_id in self.configured_ids:
            raise RuntimeError("already_configured")

    def async_create_entry(self, *, title, data, options=None):
        return {"type": "create_entry", "title": title, "data": data, "options": options}


class FakeOptionsFlow(_FakeFlowHandler):
    config_entry = None

    def async_create_entry(self, *, data):
        return {"type": "create_entry", "data": data}


def _selector_stub() -> types.ModuleType:
    """homeassistant.helpers.selector: every selector/config just records its arguments."""

    class _Recorder:
        def __init__(self, *args, **kwargs):
            self.args, self.kwargs = args, kwargs

        def __call__(self, value):   # vol.Schema needs callables
            return value

    mod = types.ModuleType("homeassistant.helpers.selector")
    for name in (
        "EntitySelector", "EntitySelectorConfig", "NumberSelector", "NumberSelectorConfig",
        "BooleanSelector", "SelectSelector", "SelectSelectorConfig",
    ):
        setattr(mod, name, type(name, (_Recorder,), {}))
    mod.NumberSelectorMode = types.SimpleNamespace(BOX="box", SLIDER="slider")
    mod.SelectSelectorMode = types.SimpleNamespace(LIST="list", DROPDOWN="dropdown")
    return mod


def load_integration() -> types.SimpleNamespace:
    """Install the HA module stubs and load the integration modules from disk.

    Returns a namespace with const, settings, init (__init__.py) and config_flow.
    """
    ha_stubs = {
        "homeassistant": types.ModuleType("homeassistant"),
        "homeassistant.core": types.ModuleType("homeassistant.core"),
        "homeassistant.const": types.ModuleType("homeassistant.const"),
        "homeassistant.helpers": types.ModuleType("homeassistant.helpers"),
        "homeassistant.helpers.event": types.ModuleType("homeassistant.helpers.event"),
        "homeassistant.helpers.config_validation": types.ModuleType("homeassistant.helpers.config_validation"),
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
        "homeassistant.helpers.selector": _selector_stub(),
    }
    ha_stubs["homeassistant.core"].HomeAssistant = object
    ha_stubs["homeassistant.core"].callback = lambda f: f
    ha_stubs["homeassistant.const"].EVENT_HOMEASSISTANT_STOP = "homeassistant_stop"
    ha_stubs["homeassistant.helpers.event"].async_track_time_interval = MagicMock(return_value=MagicMock())
    ha_stubs["homeassistant.helpers.event"].async_track_state_change_event = MagicMock(return_value=MagicMock())
    ha_stubs["homeassistant.helpers.config_validation"].config_entry_only_config_schema = lambda domain: None
    ha_stubs["homeassistant.helpers"].config_validation = ha_stubs["homeassistant.helpers.config_validation"]
    ha_stubs["homeassistant.helpers.storage"].Store = FakeStore
    config_entries = ha_stubs["homeassistant.config_entries"]
    config_entries.ConfigEntry = object
    config_entries.ConfigFlowResult = dict
    config_entries.ConfigFlow = FakeConfigFlow
    config_entries.OptionsFlow = FakeOptionsFlow
    config_entries.OptionsFlowWithReload = FakeOptionsFlow
    ha_stubs["homeassistant.helpers"].selector = ha_stubs["homeassistant.helpers.selector"]
    ha_stubs["homeassistant.helpers.device_registry"].DeviceInfo = dict

    for k, v in ha_stubs.items():
        sys.modules[k] = v

    pkg = "custom_components.ev_solar_manager"
    for key in list(sys.modules.keys()):
        if key.startswith(pkg):
            del sys.modules[key]

    base = os.path.join(os.path.dirname(__file__), "..", "custom_components", "ev_solar_manager")

    const_mod = _load_submodule(pkg, base, "const")
    settings_mod = _load_submodule(pkg, base, "settings")

    device_mod = types.ModuleType(f"{pkg}.device")
    device_mod.ev_solar_device_info = lambda: {}
    sys.modules[f"{pkg}.device"] = device_mod

    init_mod = _load_submodule(pkg, base, "__init__")
    flow_mod = _load_submodule(pkg, base, "config_flow")
    return types.SimpleNamespace(const=const_mod, settings=settings_mod, init=init_mod, config_flow=flow_mod)



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
    mod = load_integration().init

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
