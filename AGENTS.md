# AGENTS.md – EV Solar Manager

## Project Overview

Home Assistant custom integration (HACS-compatible) that adjusts EV charger current to match solar export surplus. No build system, no tests, no CI – development means editing Python files and reloading them in a live HA instance.

## Architecture

```
configuration.yaml
       │  YAML import triggers config flow
       ▼
async_setup() → async_setup_entry()   (__init__.py)
       │
       ▼
EVSolarController                     (__init__.py)
  - Reads: power_entity, voltage_entity, charger_power_entity
  - Writes: number.set_value → target_number
  - Optionally presses: charger_start_stop_button
       │
       ├── sensor.py     → computed current sensor (push via controller.register_sensor)
       ├── switch.py     → override switch + stop-on-no-injection switch
       ├── number.py     → override current number
       └── button.py     → manual recalculate button
```

All entities share a single HA device via `ev_solar_device_info()` in `device.py`.

## Key Design Decisions

- **YAML-first config**: `configuration.yaml` triggers a config flow import (`source: "import"`). There is intentionally no UI config flow – the config flow just stores YAML data as a config entry so HA can create a proper device.
- **Event-driven timer**: When `charger_status_entity` is set, the recalculation timer runs *only* while the charger is in `charging_state`. Otherwise falls back to always-on timer. This avoids API noise when unplugged.
- **Two timers**: `_unsub_timer` (active charging) and `_unsub_recovery_timer` (waiting for solar to return after stop-on-no-injection). Both are idempotent – call `_start_timer()` / `_stop_timer()` freely.
- **`_stopped_by_us` flag**: Distinguishes our stop press from user/charger-initiated stops. Only when this is `True` does the recovery timer restart charging.
- **`min_delta_amp` suppression**: `_maybe_set_current()` skips writes smaller than this threshold, except for reasons: `startup`, `charging_started`, `stop_on_no_injection_toggle`, `manual_trigger`. This ensures explicit user actions always apply immediately.
- **Charger compensation formula**: `available_w = signed_export_w + charger_consumption_w - safety_margin_w`. Real sensor preferred over estimate (`last_set_amps × V × phases`).
- **Minimum-surplus threshold**: The controller stops (or falls back to `min_current`) when `available_w < min_current × voltage × phases`. This prevents silently drawing the deficit from the grid when other appliances (e.g. washing machine) reduce solar export below the IEC 61851 minimum of 6 A worth of watts. The recovery timer uses the same threshold to decide when to restart charging. The threshold is computed dynamically using the live voltage reading (falls back to 230 V if unavailable).

## File Map

| File | Role |
|------|------|
| `__init__.py` | `async_setup`, `async_setup_entry`, `EVSolarController` (all logic) |
| `const.py` | All `CONF_*` keys, `DEFAULT_*` values, entity ID constants |
| `device.py` | `ev_solar_device_info()` – single source of truth for DeviceInfo |
| `sensor.py` | `EVComputedCurrentSensor` – calls `controller.register_sensor(self)` |
| `switch.py` | Override switch + stop-on-no-injection switch |
| `number.py` | Override current number entity |
| `button.py` | Recalculate now button |
| `config_flow.py` | Import-only flow; stores YAML dict as config entry data |
| `manifest.json` | `"requirements": []` – no PyPI deps; version must match `INTEGRATION_VERSION` in `const.py` |

## Adding New Config Options

1. Add `CONF_*` and `DEFAULT_*` to `const.py`.
2. Parse in `async_setup_entry()` in `__init__.py` and pass to `EVSolarController.__init__`.
3. If surfaced as an entity, add the entity file and register the platform in `PLATFORMS`.

## Adding New Entities

- Every entity class must call `ev_solar_device_info()` for `device_info` to stay grouped under the shared device.
- Access the controller via `hass.data[DOMAIN]["controller"]` in `async_added_to_hass`.
- Call `controller.register_sensor(self)` only for the computed-current sensor (push pattern); other entities pull state via normal HA mechanisms.

## Debugging in Home Assistant

Enable verbose logging in `configuration.yaml`:
```yaml
logger:
  logs:
    custom_components.ev_solar_manager: debug
```

All log lines are prefixed `EV Solar Manager:`. Debug lines include `power_w`, `signed_export_w`, `charger_load_w`, `available_w` per tick.

## Version Bumping

Update `version` in **both** `manifest.json` and `INTEGRATION_VERSION` in `const.py` – they must stay in sync.

## Important Constraints

- Requires Home Assistant **2024.1+** (`homeassistant.helpers.device_registry.DeviceInfo` API).
- All `.py` and `.json` files must be **UTF-8 without BOM** – a BOM causes silent load failures.
- IEC 61851 mandates minimum 6 A; `DEFAULT_MIN_CURRENT = 6` must not go below this.

