# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Home Assistant custom integration (HACS-compatible) that dynamically adjusts an EV charger's current based on real-time solar surplus. It reads grid power and voltage sensors, calculates available solar power, and writes the computed charging current to the charger via a number entity.

## Commands

```bash
# Run all tests
python -m pytest tests/ -v

# Run a single test file
python -m pytest tests/test_startup_recovery.py -v

# Run a specific test
python -m pytest tests/test_min_surplus_threshold.py::test_name -v
```

No build step — this is a Python package loaded directly by Home Assistant at runtime. No linting is configured; the project uses PyCharm's built-in checks only.

## Architecture

The integration is a single Home Assistant config entry backed by one controller class. All logic lives in `custom_components/ev_solar_manager/`.

**Entry point flow:**
1. `async_setup()` / `async_setup_entry()` in `__init__.py` — validates YAML config and creates the `EVSolarController`
2. `EVSolarController` (`__init__.py:212`) — the central state machine; owns all timers and state
3. Platform modules (`sensor.py`, `switch.py`, `number.py`, `button.py`) — thin HA entity wrappers that call back into the controller

**Controller state machine (`__init__.py`):**

The controller runs two async timers:
- `_unsub_timer` — fires every `update_interval` seconds while the charger is actively charging; runs the full recalculation
- `_unsub_recovery_timer` — fires after the charger has been stopped by the controller; polls for returning solar surplus before restarting charging

When `charger_status_entity` is configured, a state-change listener starts/stops these timers based on the charger's state. Without it, the recalculation timer runs continuously (legacy mode).

**Recalculation pipeline (each tick):**
1. Read `power_entity` (grid export, W) and `voltage_entity` (V); optionally read `charger_power_entity`
2. Compute `available_w = signed_export_w + charger_consumption_w - safety_margin_w`
3. If `available_w < min_surplus_w`: press the charger stop button (sets `_stopped_by_us = True`) and arm the recovery timer
4. Otherwise: `amps = round(available_w / (V × phases))`, clamped to `[min_current, max_current]`
5. Skip write if `|Δamps| < min_delta_amp` (suppresses noise), except for explicit user actions
6. Write to `target_number` entity; push state to the computed-current sensor

**Override mode:** When the `override` switch is ON, step 4 is skipped — `override_current` is written directly to the charger.

**`_stopped_by_us` flag:** Distinguishes controller-initiated stops from external stops. Only when this is True does the controller arm a recovery timer; otherwise it stays idle.

## Key Files

| File | Role |
|---|---|
| `__init__.py` | Controller, setup, and all calculation logic |
| `const.py` | All `CONF_*` keys and `DEFAULT_*` values — the single source of truth for config schema |
| `config_flow.py` | YAML import flow — no UI config, just imports from `configuration.yaml` |
| `sensor.py` | Exposes computed current as a push-based sensor entity |
| `switch.py` | Override and stop-on-no-injection switch entities |
| `number.py` | Override current number entity |
| `button.py` | Manual recalculate button entity |
| `manifest.json` | Integration metadata and version — must stay in sync with `INTEGRATION_VERSION` in `const.py` |

## Critical Constraints

- **Version sync**: When bumping the version, update **both** `manifest.json` and `const.py::INTEGRATION_VERSION`.
- **IEC 61851 minimum**: Charging current cannot go below 6 A. `DEFAULT_MIN_CURRENT` must remain 6.
- **No external dependencies**: `manifest.json::requirements` must remain empty — only Home Assistant APIs are allowed.
- **HA 2024.1+**: Required for the `DeviceInfo` API shape used in `device.py`.
- **UTF-8 without BOM**: All `.py` and `.json` files must be UTF-8 without BOM or the integration silently fails to load.
- **Grid meter formula**: `available = export + charger_consumption - safety_margin`. The grid meter reads net power (already including charger load), so charger consumption must be added back before computing solar surplus.

## Git Workflow

See `AGENTS.md` for the full branching strategy. Key points:
- Branch from `main`; open PRs back to `main`
- Use the local repo git identity (`ZaBug`), not any global enterprise config
- Conventional commit prefixes: `feat:`, `fix:`, `refactor:`, `chore:`, `docs:`
- Version bump commits use the tag format: `(v1.x.y)` in the message