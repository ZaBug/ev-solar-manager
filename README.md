# EV Solar Manager

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2024.1%2B-blue.svg)](https://www.home-assistant.io/)

A Home Assistant custom integration that automatically adjusts your EV charger's
charging current to match only the **solar surplus** exported to the grid — so you
charge your car for free with excess solar power instead of buying it from the grid.

A manual **override mode** lets you lock the charger to a fixed current whenever
you need full-speed charging regardless of solar production.

---

## How it works

```
┌─────────────────┐    every N seconds    ┌──────────────────────────────────────┐
│  Grid Power     │ ──────────────────►   │  EVSolarController                   │
│  sensor (W)     │                       │                                      │
├─────────────────┤                       │  available = export + charger_load   │
│  Grid Voltage   │ ──────────────────►   │            - safety_margin           │
│  sensor (V)     │                       │                                      │
├─────────────────┤                       │  I = available / (U × phases)        │
│  Charger Power  │ ──────────────────►   │  clamp(min_current, I, max_current)  │
│  sensor (W) opt │                       │                                      │
└─────────────────┘                       └──────────────┬───────────────────────┘
                                                         │ number.set_value
                                          ┌──────────────▼───────────────────────┐
                                          │  EV Charger Number entity            │
                                          │  (target current in Amperes)         │
                                          └──────────────────────────────────────┘
```

1. Every `update_interval` seconds the controller reads the **grid power sensor**.
2. It compensates for the EV charger's own consumption (which is already embedded
   in the grid meter reading) to find the true available solar budget:
   ```
   available_watts = grid_export_watts + charger_consumption_watts - safety_margin_w
   ```
   - `charger_consumption_watts` comes from a real sensor (`charger_power_entity`) if
     configured, otherwise it is estimated as `last_set_amps × voltage × phases`.
3. The target current is calculated and **clamped** between `min_current` and `max_current`:
   ```
   charging_amps = round(available_watts / (grid_voltage × phases))
   ```
4. The value is written to the charger entity **only** if the change is at least
   `min_delta_amp` Amperes — to avoid hammering the charger with tiny adjustments.
5. When no solar budget is available the charger is set to `min_current`.

### Override mode

Turn on `switch.ev_solar_manager_override` to lock the charger to the current set
in `number.ev_solar_manager_override_current`. Solar logic is paused until the
switch is turned off again.

### Manual recalculation

Press `button.ev_solar_manager_recalculate_now` to trigger an immediate
recalculation without waiting for the next `update_interval` tick. Useful for
testing and debugging.

---

## Entities created

| Entity | Type | Description |
|--------|------|-------------|
| `sensor.ev_solar_manager_computed_current` | Sensor (A) | Last current calculated from solar data |
| `switch.ev_solar_manager_override` | Switch | Enable / disable manual override |
| `number.ev_solar_manager_override_current` | Number (A) | Manual current for override mode |
| `button.ev_solar_manager_recalculate_now` | Button | Trigger an immediate recalculation |

All entities are grouped under a single **EV Solar Manager** device in HA.

---

## Requirements

- Home Assistant **2024.1** or later
- An **EV charger** integration that exposes a `number` entity to set the max current
  (e.g. [Duosida](https://github.com/example/duosida), go-e Charger, Wallbox, …)
- A **grid power sensor** that reports:
  - **negative Watts** when your solar system is exporting to the grid *(most bidirectional meters)*
  - **or positive Watts** if you use a dedicated production sensor *(set `export_is_negative: false`)*
- A **grid voltage sensor** reporting AC voltage in Volts
- *(Optional)* A **charger power sensor** (e.g. Shelly EM) for more accurate compensation

---

## Installation

### Via HACS (recommended)

1. Open **HACS → Integrations → ⋮ → Custom repositories**
2. Add this repository URL and select category **Integration**
3. Search for **EV Solar Manager** and install it
4. Restart Home Assistant

### Manual

1. Copy the `custom_components/ev_solar_manager` folder to your HA config directory:
   ```
   config/
   └── custom_components/
       └── ev_solar_manager/
           ├── __init__.py
           ├── button.py
           ├── const.py
           ├── manifest.json
           ├── number.py
           ├── sensor.py
           └── switch.py
   ```
2. Restart Home Assistant

---

## Configuration

Add the following block to your `configuration.yaml`:

```yaml
ev_solar_manager:
  # --- Required ---
  power_entity: sensor.principal_power          # grid power sensor entity ID
  voltage_entity: sensor.principal_voltage      # grid voltage sensor entity ID
  target_number: number.duosida_set_maximal_current  # charger current number entity ID

  # --- Optional ---
  min_current: 6          # minimum charging current in A (default: 6)
  max_current: 24         # maximum charging current in A (default: 24)
  update_interval: 60     # recalculation interval in seconds (default: 60)
  min_delta_amp: 1        # minimum change in A before writing to charger (default: 1)
  export_is_negative: true   # true if grid sensor is negative when exporting (default: true)
  phases: 1               # charging phases – 1 for single-phase, 3 for three-phase (default: 1)
  charger_power_entity: sensor.shellyem3_xxxx_channel_b_power  # real charger power sensor (W)
  safety_margin_w: 100    # keep this many Watts as buffer to avoid grid import (default: 0)
```

### Enable debug logging

```yaml
logger:
  default: warning
  logs:
    custom_components.ev_solar_manager: debug
```

---

## Configuration reference

| Key | Required | Default | Description |
|-----|----------|---------|-------------|
| `power_entity` | ✅ | — | Entity ID of the grid power sensor (W) |
| `voltage_entity` | ✅ | — | Entity ID of the grid voltage sensor (V) |
| `target_number` | ✅ | — | Entity ID of the charger's max-current number entity |
| `min_current` | ❌ | `6` | Minimum charging current in A. IEC 61851 mandates ≥ 6 A |
| `max_current` | ❌ | `24` | Maximum charging current in A |
| `update_interval` | ❌ | `60` | How often to re-read sensors and recalculate (seconds) |
| `min_delta_amp` | ❌ | `1` | Suppresses writes when the change is smaller than this (A) |
| `export_is_negative` | ❌ | `true` | Set to `false` if your power sensor is **positive** when exporting |
| `phases` | ❌ | `1` | Set to `3` if your charger operates on three-phase AC |
| `charger_power_entity` | ❌ | — | Real-time charger power sensor (W). When set, used instead of the estimated value for charger compensation. Recommended for best accuracy. |
| `safety_margin_w` | ❌ | `0` | Watts to keep as buffer. Set to e.g. `100` to always inject ≥100 W to the grid and avoid accidental import. |

### How to determine `export_is_negative`

Go to **Developer Tools → States** and look at your power sensor while solar
production exceeds consumption:

- Value is `-1500` → sensor is negative when exporting → `export_is_negative: true` ✅
- Value is `+1500` → sensor is positive when exporting → `export_is_negative: false`

### Why use `charger_power_entity`?

The grid meter measures the **net** power at the connection point, which already
includes the EV charger's consumption. Without knowing what the charger is actually
drawing, the controller would underestimate the available solar budget.

With `charger_power_entity` set to a real energy monitor (e.g. a Shelly EM channel
on the charger circuit), the controller uses the **measured** charger power instead
of an estimate, resulting in more accurate and stable current adjustments.

### Dashboard card example

```yaml
type: entities
title: EV Solar Manager
icon: mdi:solar-power
entities:
  - entity: sensor.ev_solar_manager_computed_current
    name: Computed current
  - entity: switch.ev_solar_manager_override
    name: Manual override
  - entity: number.ev_solar_manager_override_current
    name: Override current
  - entity: button.ev_solar_manager_recalculate_now
    name: Recalculate now
    icon: mdi:refresh
```

---

## Troubleshooting

### The charger current never changes

1. Enable debug logging (see above) and look for lines from `custom_components.ev_solar_manager`.
2. Verify the sign of `power_entity` — use Developer Tools → States while solar is producing.
3. Confirm `target_number` matches exactly the entity ID in Developer Tools.
4. Make sure the power sensor is not `unavailable` or `unknown`.

### The charger is always set to `min_current`

Your power sensor is probably **positive** when exporting. Add `export_is_negative: false`.

### The current still leaves unused solar surplus

Add `charger_power_entity` pointing to a real energy monitor on the charger circuit.
This prevents the controller from underestimating what the charger already consumes.

### The current jumps too aggressively

Increase `min_delta_amp` (e.g. `2` or `3`) to reduce the frequency of changes.  
Increase `safety_margin_w` (e.g. `200`) to add a stable buffer.

### The component fails to load at startup

Check **Settings → System → Logs** for errors from `ev_solar_manager`.
Common causes:
- A typo in an entity ID
- A required key missing from configuration
- Incompatible Home Assistant version (requires 2024.1+)
- UTF-8 BOM in a `.py` or `.json` file (save all files as UTF-8 **without** BOM)

---

## Example automation: stop charging at night

```yaml
automation:
  - alias: "Stop EV charging after sunset"
    trigger:
      - platform: sun
        event: sunset
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.ev_solar_manager_override
      - service: number.set_value
        target:
          entity_id: number.ev_solar_manager_override_current
        data:
          value: 6
```

---

## Contributing

Pull requests and issues are welcome. Please include relevant log lines and your
`configuration.yaml` snippet (without secrets) when reporting a bug.

---

## License

MIT

