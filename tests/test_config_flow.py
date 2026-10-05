"""Tests for the UI config flow, the options flow and the YAML → UI entry migration.

Run with:
    python -m pytest tests/test_config_flow.py -v
"""

from __future__ import annotations

import types

import pytest

from tests.conftest import FakeHass, load_integration


SENSORS_INPUT = {
    "power_entity": "sensor.grid_power",
    "export_is_negative": True,
    "voltage_entity": "sensor.grid_voltage",
    "target_number": "number.charger_current",
}

TUNING_INPUT = {
    "min_current": 6.0,
    "max_current": 32.0,
    "phases": "1",
    "safety_margin_w": 30.0,
    "update_interval": 60.0,
    "min_delta_amp": 1.0,
    "start_hysteresis_w": 200.0,
    "stop_delay_s": 120.0,
    "start_delay_s": 120.0,
}

# Raw YAML block as stored by the v1.x import flow
YAML_DATA = {
    "power_entity": "sensor.grid_power",
    "voltage_entity": "sensor.grid_voltage",
    "target_number": "number.duosida_max_current",
    "max_current": 32,
    "charger_power_entity": "sensor.charger_power",
    "safety_margin_w": 30,
    "charger_status_entity": "sensor.duosida_status",
    "charging_state": "charging",
    "charger_start_stop_button": "button.duosida_start_stop",
    "stopped_state": "finishing",
    "stop_delay_s": 300,
}


@pytest.fixture
def integration():
    return load_integration()


def _config_flow(integration, states: dict | None = None, attributes: dict | None = None):
    flow = integration.config_flow.EVSolarManagerConfigFlow()
    flow.hass = FakeHass(states or {})
    flow.hass.states._attributes = attributes or {}
    return flow


def _options_flow(integration, options: dict, states: dict | None = None):
    flow = integration.config_flow.EVSolarManagerOptionsFlow()
    flow.hass = FakeHass(states or {})
    flow.config_entry = types.SimpleNamespace(options=options)
    return flow


# ---------------------------------------------------------------------------
# Settings normalization / validation
# ---------------------------------------------------------------------------

def test_normalize_fills_defaults_and_drops_unknown_keys(integration):
    settings = integration.settings.normalize({**YAML_DATA, "foo": "bar", "charger_power_entity": " "})

    assert "foo" not in settings
    assert "charger_power_entity" not in settings, "empty optional entity must be dropped"
    assert settings["max_current"] == 32
    assert settings["min_current"] == 6
    assert settings["export_is_negative"] is True
    assert settings["stop_delay_s"] == 300.0
    assert settings["start_hysteresis_w"] == 200.0


def test_normalize_coerces_selector_values(integration):
    settings = integration.settings.normalize({**SENSORS_INPUT, **TUNING_INPUT, "export_is_negative": "false"})

    assert settings["phases"] == 1 and isinstance(settings["phases"], int)
    assert settings["max_current"] == 32 and isinstance(settings["max_current"], int)
    assert settings["export_is_negative"] is False


def test_validate_rules(integration):
    settings = integration.settings
    ok = settings.normalize({**SENSORS_INPUT, **TUNING_INPUT})
    assert settings.validate(ok) == {}

    errors = settings.validate({**ok, "charger_start_stop_button": "button.x", "max_current": 5, "phases": 2})
    assert errors == {
        "charger_start_stop_button": "button_needs_status",
        "max_current": "max_below_min",
        "phases": "invalid_phases",
    }
    assert settings.validate({**ok, "min_current": 5})["min_current"] == "min_current_too_low"
    assert settings.validate(settings.normalize({}))["power_entity"] == "required"


# ---------------------------------------------------------------------------
# Migration of v1 (YAML import) entries
# ---------------------------------------------------------------------------

class _FakeConfigEntries:
    def async_update_entry(self, entry, *, data=None, options=None, version=None):
        entry.data, entry.options, entry.version = data, options, version


@pytest.mark.asyncio
async def test_migrate_v1_moves_yaml_data_to_options(integration):
    hass = types.SimpleNamespace(config_entries=_FakeConfigEntries())
    entry = types.SimpleNamespace(version=1, data=dict(YAML_DATA), options={})

    assert await integration.init.async_migrate_entry(hass, entry) is True

    assert entry.version == 2
    assert entry.data == {}
    assert entry.options == integration.settings.normalize(YAML_DATA)
    assert entry.options["charging_state"] == "charging"
    assert entry.options["stopped_state"] == "finishing"
    assert entry.options["charger_start_stop_button"] == "button.duosida_start_stop"


@pytest.mark.asyncio
async def test_migrate_rejects_newer_version(integration):
    entry = types.SimpleNamespace(version=3, data={}, options={})
    assert await integration.init.async_migrate_entry(types.SimpleNamespace(), entry) is False


@pytest.mark.asyncio
async def test_setup_entry_fails_without_required_entities(integration):
    entry = types.SimpleNamespace(options={"power_entity": "sensor.grid_power"})
    assert await integration.init.async_setup_entry(types.SimpleNamespace(), entry) is False


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_config_flow_minimal_setup(integration):
    flow = _config_flow(integration)

    result = await flow.async_step_user()
    assert result["type"] == "form" and result["step_id"] == "user"
    assert result["data_schema"].suggested["export_is_negative"] is True

    result = await flow.async_step_user(SENSORS_INPUT)
    assert result["step_id"] == "charger"

    result = await flow.async_step_charger({})
    assert result["step_id"] == "tuning", "states step is skipped without a status sensor"
    assert result["data_schema"].suggested["phases"] == "1"
    assert result["data_schema"].suggested["min_current"] == 6

    result = await flow.async_step_tuning(TUNING_INPUT)
    assert result["type"] == "create_entry"
    assert result["data"] == {}
    assert result["options"] == integration.settings.normalize({**SENSORS_INPUT, **TUNING_INPUT})
    assert "charger_status_entity" not in result["options"]


@pytest.mark.asyncio
async def test_config_flow_offers_status_values_of_enum_sensor(integration):
    flow = _config_flow(
        integration,
        states={"sensor.duosida_status": "available"},
        attributes={"sensor.duosida_status": {"options": ["available", "charging", "finishing"]}},
    )
    await flow.async_step_user(SENSORS_INPUT)

    result = await flow.async_step_charger(
        {"charger_status_entity": "sensor.duosida_status", "charger_start_stop_button": "button.duosida_start_stop"}
    )
    assert result["step_id"] == "states"
    choices = result["data_schema"].schema
    status_selector = next(iter(choices.values()))
    assert status_selector.args[0].kwargs["options"] == ["available", "charging", "finishing", "Charging", "Stopped"]
    assert status_selector.args[0].kwargs["custom_value"] is True

    result = await flow.async_step_states({"charging_state": "charging", "stopped_state": "finishing"})
    assert result["step_id"] == "tuning"

    result = await flow.async_step_tuning(TUNING_INPUT)
    options = result["options"]
    assert options["charging_state"] == "charging"
    assert options["stopped_state"] == "finishing"
    assert options["charger_start_stop_button"] == "button.duosida_start_stop"


@pytest.mark.asyncio
async def test_config_flow_button_requires_status_sensor(integration):
    flow = _config_flow(integration)
    await flow.async_step_user(SENSORS_INPUT)

    result = await flow.async_step_charger({"charger_start_stop_button": "button.duosida_start_stop"})

    assert result["type"] == "form" and result["step_id"] == "charger"
    assert result["errors"] == {"charger_start_stop_button": "button_needs_status"}


@pytest.mark.asyncio
async def test_config_flow_max_below_min_is_rejected(integration):
    flow = _config_flow(integration)
    await flow.async_step_user(SENSORS_INPUT)
    await flow.async_step_charger({})

    result = await flow.async_step_tuning({**TUNING_INPUT, "min_current": 10.0, "max_current": 8.0})

    assert result["type"] == "form" and result["step_id"] == "tuning"
    assert result["errors"] == {"max_current": "max_below_min"}


@pytest.mark.asyncio
async def test_config_flow_single_instance(integration):
    flow = _config_flow(integration)
    flow.configured_ids = {integration.const.DOMAIN}
    with pytest.raises(RuntimeError, match="already_configured"):
        await flow.async_step_user()


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_options_flow_prefills_and_keeps_unchanged_settings(integration):
    current = integration.settings.normalize(YAML_DATA)
    flow = _options_flow(integration, current)

    result = await flow.async_step_init()
    assert result["step_id"] == "init"
    assert result["data_schema"].suggested["target_number"] == "number.duosida_max_current"

    await flow.async_step_init({**SENSORS_INPUT, "target_number": "number.duosida_max_current"})
    result = await flow.async_step_charger({
        "charger_power_entity": "sensor.charger_power",
        "charger_status_entity": "sensor.duosida_status",
        "charger_start_stop_button": "button.duosida_start_stop",
    })
    assert result["data_schema"].suggested == {"charging_state": "charging", "stopped_state": "finishing"}

    await flow.async_step_states({"charging_state": "charging", "stopped_state": "finishing"})
    result = await flow.async_step_tuning({**TUNING_INPUT, "stop_delay_s": 300.0})

    assert result["type"] == "create_entry"
    assert result["data"] == current, "re-saving unchanged values must not alter the settings"


@pytest.mark.asyncio
async def test_options_flow_clearing_optional_entity_removes_it(integration):
    flow = _options_flow(integration, integration.settings.normalize(YAML_DATA))
    await flow.async_step_init()
    await flow.async_step_init({**SENSORS_INPUT})

    # Status sensor and button cleared → the states step is skipped
    result = await flow.async_step_charger({"charger_power_entity": "sensor.charger_power"})
    assert result["step_id"] == "tuning"

    result = await flow.async_step_tuning(TUNING_INPUT)
    assert "charger_status_entity" not in result["data"]
    assert "charger_start_stop_button" not in result["data"]
    assert result["data"]["charger_power_entity"] == "sensor.charger_power"
