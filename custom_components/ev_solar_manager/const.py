"""Constants for EV Solar Manager."""

from __future__ import annotations

DOMAIN = "ev_solar_manager"
INTEGRATION_VERSION = "1.0.0"  # keep in sync with manifest.json

# --- Configuration keys ---
CONF_TARGET_NUMBER = "target_number"
CONF_POWER_ENTITY = "power_entity"
CONF_VOLTAGE_ENTITY = "voltage_entity"
CONF_MIN_CURRENT = "min_current"
CONF_MAX_CURRENT = "max_current"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_MIN_DELTA_AMP = "min_delta_amp"
CONF_EXPORT_IS_NEGATIVE = "export_is_negative"
CONF_PHASES = "phases"
CONF_CHARGER_POWER_ENTITY = "charger_power_entity"  # optional: real charger power sensor (W)
CONF_SAFETY_MARGIN_W = "safety_margin_w"            # optional: watts to keep as buffer
CONF_CHARGER_STATUS_ENTITY = "charger_status_entity"  # optional: charger status sensor entity ID
CONF_CHARGING_STATE = "charging_state"                # optional: state string that means "actively charging"
CONF_CHARGER_START_STOP_BUTTON = "charger_start_stop_button"  # optional: toggle button entity ID
CONF_STOPPED_STATE = "stopped_state"                  # optional: state string that means "stopped by us"

# --- Defaults ---
DEFAULT_MIN_CURRENT = 6        # Minimum charging current (A) – IEC 61851 minimum is 6 A
DEFAULT_MAX_CURRENT = 24       # Maximum charging current (A)
DEFAULT_UPDATE_INTERVAL = 60   # Recalculation interval (seconds)
DEFAULT_MIN_DELTA_AMP = 1      # Only write to charger if the change is >= this value (A)
DEFAULT_EXPORT_IS_NEGATIVE = True  # True → grid sensor is negative when exporting solar surplus
DEFAULT_PHASES = 1             # Number of AC phases used for charging (1 or 3)
DEFAULT_SAFETY_MARGIN_W = 0.0  # No safety buffer by default
DEFAULT_CHARGING_STATE = "Charging"  # Default charger status string that means actively charging
DEFAULT_STOPPED_STATE = "Stopped"    # Default charger status string that means stopped/waiting

# --- Entity IDs created by this integration ---
SWITCH_OVERRIDE_ENTITY = "switch.ev_solar_manager_override"
SWITCH_STOP_ON_NO_INJECTION_ENTITY = "switch.ev_solar_manager_stop_on_no_injection"
NUMBER_OVERRIDE_ENTITY = "number.ev_solar_manager_override_current"
SENSOR_COMPUTED_ENTITY = "sensor.ev_solar_manager_computed_current"
BUTTON_RECALC_ENTITY = "button.ev_solar_manager_recalculate_now"
SENSOR_CHARGING_STATUS_ENTITY = "binary_sensor.ev_solar_manager_is_charging"
