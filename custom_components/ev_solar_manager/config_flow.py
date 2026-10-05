"""Config flow for EV Solar Manager.

Setup and the options flow share the same steps:

  1. sensors  – grid power / voltage sensors and the charger current number
  2. charger  – optional charger power sensor, status sensor and start/stop button
  3. states   – status values for "charging" and "stopped" (only with a status sensor)
  4. tuning   – currents, phases, timing and anti-flapping parameters

All settings are stored in the entry options; saving the options reloads the entry.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_CHARGER_POWER_ENTITY,
    CONF_CHARGER_START_STOP_BUTTON,
    CONF_CHARGER_STATUS_ENTITY,
    CONF_CHARGING_STATE,
    CONF_EXPORT_IS_NEGATIVE,
    CONF_MAX_CURRENT,
    CONF_MIN_CURRENT,
    CONF_MIN_DELTA_AMP,
    CONF_PHASES,
    CONF_POWER_ENTITY,
    CONF_SAFETY_MARGIN_W,
    CONF_START_DELAY_S,
    CONF_START_HYSTERESIS_W,
    CONF_STOP_DELAY_S,
    CONF_STOPPED_STATE,
    CONF_TARGET_NUMBER,
    CONF_UPDATE_INTERVAL,
    CONF_VOLTAGE_ENTITY,
    DOMAIN,
    IEC_MIN_CURRENT,
)
from .settings import normalize, validate

_TITLE = "EV Solar Manager"
_UNAVAILABLE_STATES: frozenset[str] = frozenset({"unavailable", "unknown"})


def _entity(domain: str) -> selector.EntitySelector:
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=domain))


def _number(minimum: float, maximum: float, unit: str, step: float = 1) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=step,
            unit_of_measurement=unit,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


SENSORS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_POWER_ENTITY): _entity("sensor"),
        vol.Required(CONF_EXPORT_IS_NEGATIVE): selector.BooleanSelector(),
        vol.Required(CONF_VOLTAGE_ENTITY): _entity("sensor"),
        vol.Required(CONF_TARGET_NUMBER): _entity("number"),
    }
)

CHARGER_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_CHARGER_POWER_ENTITY): _entity("sensor"),
        vol.Optional(CONF_CHARGER_STATUS_ENTITY): _entity("sensor"),
        vol.Optional(CONF_CHARGER_START_STOP_BUTTON): _entity("button"),
    }
)

TUNING_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_MIN_CURRENT): _number(IEC_MIN_CURRENT, 80, "A"),
        vol.Required(CONF_MAX_CURRENT): _number(IEC_MIN_CURRENT, 80, "A"),
        vol.Required(CONF_PHASES): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=["1", "3"],
                mode=selector.SelectSelectorMode.LIST,
                translation_key="phases",
            )
        ),
        vol.Required(CONF_SAFETY_MARGIN_W): _number(0, 5000, "W", 10),
        vol.Required(CONF_UPDATE_INTERVAL): _number(10, 3600, "s"),
        vol.Required(CONF_MIN_DELTA_AMP): _number(1, 10, "A"),
        vol.Required(CONF_START_HYSTERESIS_W): _number(0, 5000, "W", 10),
        vol.Required(CONF_STOP_DELAY_S): _number(0, 3600, "s"),
        vol.Required(CONF_START_DELAY_S): _number(0, 3600, "s"),
    }
)


class _SettingsSteps:
    """Steps shared by the config flow and the options flow.

    Collects the user input in self._settings; _async_finish() stores it.
    """

    _settings: dict[str, Any]

    async def _async_finish(self) -> ConfigFlowResult:
        raise NotImplementedError

    def _form(self, step_id: str, schema: vol.Schema, errors: dict[str, str] | None = None) -> ConfigFlowResult:
        suggested = dict(self._settings)
        if CONF_PHASES in suggested:
            suggested[CONF_PHASES] = str(suggested[CONF_PHASES])   # select options are strings
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors or {},
        )

    def _merge(self, schema: vol.Schema, user_input: dict[str, Any]) -> None:
        # A cleared optional field is missing from user_input: drop the old value.
        for key in schema.schema:
            self._settings.pop(str(key), None)
        self._settings.update({key: value for key, value in user_input.items() if value not in (None, "")})

    async def _async_step_sensors(self, step_id: str, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        if user_input is not None:
            self._merge(SENSORS_SCHEMA, user_input)
            return await self.async_step_charger()
        return self._form(step_id, SENSORS_SCHEMA)

    async def async_step_charger(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._merge(CHARGER_SCHEMA, user_input)
            errors = {
                key: error
                for key, error in validate(normalize(self._settings)).items()
                if key in CHARGER_SCHEMA.schema
            }
            if not errors:
                if self._settings.get(CONF_CHARGER_STATUS_ENTITY):
                    return await self.async_step_states()
                return await self.async_step_tuning()
        return self._form("charger", CHARGER_SCHEMA, errors)

    async def async_step_states(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._merge(self._states_schema(), user_input)
            return await self.async_step_tuning()
        return self._form("states", self._states_schema())

    def _states_schema(self) -> vol.Schema:
        """Offer the values the status sensor reports (enum options, current state)."""
        choices: list[str] = []
        state = self.hass.states.get(self._settings.get(CONF_CHARGER_STATUS_ENTITY, ""))
        if state is not None:
            choices.extend(str(option) for option in state.attributes.get("options") or [])
            if state.state not in _UNAVAILABLE_STATES:
                choices.append(state.state)
        defaults = normalize(self._settings)
        choices.extend((defaults[CONF_CHARGING_STATE], defaults[CONF_STOPPED_STATE]))
        status_selector = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=list(dict.fromkeys(choices)),
                custom_value=True,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )
        return vol.Schema(
            {
                vol.Required(CONF_CHARGING_STATE): status_selector,
                vol.Required(CONF_STOPPED_STATE): status_selector,
            }
        )

    async def async_step_tuning(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._merge(TUNING_SCHEMA, user_input)
            errors = {
                key: error
                for key, error in validate(normalize(self._settings)).items()
                if key in TUNING_SCHEMA.schema
            }
            if not errors:
                return await self._async_finish()
        return self._form("tuning", TUNING_SCHEMA, errors)


class EVSolarManagerConfigFlow(_SettingsSteps, ConfigFlow, domain=DOMAIN):
    """Set up EV Solar Manager (single instance)."""

    VERSION = 2

    def __init__(self) -> None:
        self._settings = normalize({})

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return await self._async_step_sensors("user", user_input)

    async def _async_finish(self) -> ConfigFlowResult:
        return self.async_create_entry(title=_TITLE, data={}, options=normalize(self._settings))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EVSolarManagerOptionsFlow()


class EVSolarManagerOptionsFlow(_SettingsSteps, OptionsFlowWithReload):
    """Change any setting. Saving reloads the entry; the stopped-by-us state is kept."""

    def __init__(self) -> None:
        self._settings = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if not self._settings:
            self._settings = normalize(self.config_entry.options)
        return await self._async_step_sensors("init", user_input)

    async def _async_finish(self) -> ConfigFlowResult:
        return self.async_create_entry(data=normalize(self._settings))
