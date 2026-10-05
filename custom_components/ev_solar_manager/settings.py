"""Normalization and validation of EV Solar Manager settings.

Kept free of Home Assistant imports so the rules can be unit-tested and shared
by async_setup_entry, the config/options flow and the entry migration.
"""

from __future__ import annotations

from typing import Any

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
    DEFAULT_CHARGING_STATE,
    DEFAULT_EXPORT_IS_NEGATIVE,
    DEFAULT_MAX_CURRENT,
    DEFAULT_MIN_CURRENT,
    DEFAULT_MIN_DELTA_AMP,
    DEFAULT_PHASES,
    DEFAULT_SAFETY_MARGIN_W,
    DEFAULT_START_DELAY_S,
    DEFAULT_START_HYSTERESIS_W,
    DEFAULT_STOP_DELAY_S,
    DEFAULT_STOPPED_STATE,
    DEFAULT_UPDATE_INTERVAL,
    IEC_MIN_CURRENT,
)

REQUIRED_ENTITIES: tuple[str, ...] = (CONF_POWER_ENTITY, CONF_VOLTAGE_ENTITY, CONF_TARGET_NUMBER)
OPTIONAL_ENTITIES: tuple[str, ...] = (
    CONF_CHARGER_POWER_ENTITY,
    CONF_CHARGER_STATUS_ENTITY,
    CONF_CHARGER_START_STOP_BUTTON,
)

# Setting key → (type, default). Entity ids are handled separately.
_TYPED_DEFAULTS: dict[str, tuple[type, Any]] = {
    CONF_EXPORT_IS_NEGATIVE: (bool, DEFAULT_EXPORT_IS_NEGATIVE),
    CONF_CHARGING_STATE: (str, DEFAULT_CHARGING_STATE),
    CONF_STOPPED_STATE: (str, DEFAULT_STOPPED_STATE),
    CONF_MIN_CURRENT: (int, DEFAULT_MIN_CURRENT),
    CONF_MAX_CURRENT: (int, DEFAULT_MAX_CURRENT),
    CONF_PHASES: (int, DEFAULT_PHASES),
    CONF_UPDATE_INTERVAL: (int, DEFAULT_UPDATE_INTERVAL),
    CONF_MIN_DELTA_AMP: (int, DEFAULT_MIN_DELTA_AMP),
    CONF_SAFETY_MARGIN_W: (float, DEFAULT_SAFETY_MARGIN_W),
    CONF_START_HYSTERESIS_W: (float, DEFAULT_START_HYSTERESIS_W),
    CONF_STOP_DELAY_S: (float, DEFAULT_STOP_DELAY_S),
    CONF_START_DELAY_S: (float, DEFAULT_START_DELAY_S),
}

ALL_KEYS: frozenset[str] = frozenset(
    {*REQUIRED_ENTITIES, *OPTIONAL_ENTITIES, *_TYPED_DEFAULTS}
)


def _coerce(kind: type, value: Any) -> Any:
    if kind is bool:
        if isinstance(value, str):
            return value.strip().lower() in ("true", "yes", "on", "1")
        return bool(value)
    if kind is int:
        return int(round(float(value)))
    if kind is float:
        return float(value)
    return str(value).strip()


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a complete settings dict: known keys only, typed, defaults filled in.

    Empty optional entity ids are dropped. Values that cannot be converted fall
    back to their default, so a stored entry never prevents setup by itself.
    """
    settings: dict[str, Any] = {}
    for key in (*REQUIRED_ENTITIES, *OPTIONAL_ENTITIES):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            settings[key] = value.strip()
    for key, (kind, default) in _TYPED_DEFAULTS.items():
        value = raw.get(key)
        try:
            settings[key] = default if value is None or value == "" else _coerce(kind, value)
        except (TypeError, ValueError):
            settings[key] = default
    if not settings[CONF_CHARGING_STATE]:
        settings[CONF_CHARGING_STATE] = DEFAULT_CHARGING_STATE
    if not settings[CONF_STOPPED_STATE]:
        settings[CONF_STOPPED_STATE] = DEFAULT_STOPPED_STATE
    return settings


def validate(settings: dict[str, Any]) -> dict[str, str]:
    """Check cross-field rules. Returns {field: error_key}; empty when valid."""
    errors: dict[str, str] = {}
    for key in REQUIRED_ENTITIES:
        if not settings.get(key):
            errors[key] = "required"
    if settings.get(CONF_CHARGER_START_STOP_BUTTON) and not settings.get(CONF_CHARGER_STATUS_ENTITY):
        errors[CONF_CHARGER_START_STOP_BUTTON] = "button_needs_status"
    min_current = settings.get(CONF_MIN_CURRENT, DEFAULT_MIN_CURRENT)
    if min_current < IEC_MIN_CURRENT:
        errors[CONF_MIN_CURRENT] = "min_current_too_low"
    if settings.get(CONF_MAX_CURRENT, DEFAULT_MAX_CURRENT) < min_current:
        errors[CONF_MAX_CURRENT] = "max_below_min"
    if settings.get(CONF_PHASES, DEFAULT_PHASES) not in (1, 3):
        errors[CONF_PHASES] = "invalid_phases"
    return errors
