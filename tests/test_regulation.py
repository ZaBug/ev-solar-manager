"""Tests for current regulation: closed loop on the grid meter and grid power averaging.

Scenario that motivated the closed loop (live data, 1-phase, safety_margin_w=30):
the charger draws only ~87 % of its setpoint. The open-loop formula
I = (export + charger_load) / V then settles at a fixed point where ~450 W keep
being exported (14 A set → 2800 W drawn → 450 W export → 3220 W / 230 V = 14 A again).
The closed loop corrects the last setpoint by the remaining export instead.

Run with:
    python -m pytest tests/test_regulation.py -v
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock

import pytest

from tests.conftest import FakeState, make_controller

SURPLUS_W = 3250        # solar surplus without the car
CHARGER_RATIO = 0.87    # charger draws 87 % of its setpoint
VOLTAGE = 230


def last_set_value(hass) -> float | None:
    values = [c["data"]["value"] for c in hass.services.calls if c["service"] == "set_value"]
    return values[-1] if values else None


def set_power(ctrl, hass, value: float) -> None:
    old = hass.states.get("sensor.grid_power")
    hass.states.set("sensor.grid_power", value)
    ctrl._handle_power_change(types.SimpleNamespace(data={
        "old_state": old,
        "new_state": FakeState(str(value)),
    }))


def apply_plant(ctrl, hass, amps: float) -> float:
    """Simulate house + charger: charger draws CHARGER_RATIO of the setpoint. Returns export W."""
    draw_w = amps * VOLTAGE * CHARGER_RATIO
    export_w = SURPLUS_W - draw_w
    hass.states.set("sensor.charger_power", round(draw_w, 1))
    set_power(ctrl, hass, round(-export_w, 1))   # export_is_negative
    return export_w


def make_regulated(last_set: int | None = 14):
    states = {
        "sensor.grid_power": 0,
        "sensor.grid_voltage": VOLTAGE,
        "sensor.charger_status": "Charging",
        "sensor.charger_power": 0,
    }
    ctrl, hass, _ = make_controller(
        states, safety_margin_w=30, charger_power_entity="sensor.charger_power",
    )
    clock = [1000.0]
    ctrl._monotonic = lambda: clock[0]
    ctrl._is_charging = True
    ctrl._last_set_current = last_set
    return ctrl, hass, clock


# ---------------------------------------------------------------------------
# Closed loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_closed_loop_absorbs_export_when_charger_draws_less_than_setpoint():
    ctrl, hass, clock = make_regulated(last_set=14)
    export_w = apply_plant(ctrl, hass, 14)
    assert export_w > 400, "Starting point: ~450 W exported at 14 A"

    for _ in range(5):
        clock[0] += 60
        await ctrl._compute_and_apply("timer")
        export_w = apply_plant(ctrl, hass, ctrl._last_set_current)

    assert ctrl._last_set_current == 16
    assert export_w < 150, f"Export should converge near the 30 W target, got {export_w:.0f} W"


@pytest.mark.asyncio
async def test_small_error_inside_deadband_keeps_current():
    """Error of ±0.5 A must not change the setpoint (no 11↔12 A flapping)."""
    ctrl, hass, clock = make_regulated(last_set=12)
    hass.states.set("sensor.charger_power", 2400)

    for export_w in (30 + 0.5 * VOLTAGE, 30 - 0.5 * VOLTAGE, 30 + 0.4 * VOLTAGE):
        set_power(ctrl, hass, -export_w)
        clock[0] += 60
        await ctrl._compute_and_apply("timer")
        assert ctrl._last_set_current == 12

    assert hass.services.count("set_value") == 0


@pytest.mark.asyncio
async def test_step_is_limited_to_three_amps():
    ctrl, hass, clock = make_regulated(last_set=6)
    hass.states.set("sensor.charger_power", 1200)
    set_power(ctrl, hass, -3000)          # +12.9 A worth of export
    clock[0] += 60

    await ctrl._compute_and_apply("timer")

    assert last_set_value(hass) == 9.0


@pytest.mark.asyncio
async def test_import_reduces_current():
    ctrl, hass, clock = make_regulated(last_set=14)
    hass.states.set("sensor.charger_power", 2800)
    set_power(ctrl, hass, 300)            # importing 300 W → (−300 − 30) / 230 = −1.4 A
    clock[0] += 60

    await ctrl._compute_and_apply("timer")

    assert last_set_value(hass) == 13.0


@pytest.mark.asyncio
async def test_first_tick_without_previous_write_uses_open_loop():
    ctrl, hass, clock = make_regulated(last_set=None)
    hass.states.set("sensor.charger_power", 2800)
    set_power(ctrl, hass, -450)
    clock[0] += 60

    await ctrl._compute_and_apply("charging_started")

    # (450 + 2800 − 30) / 230 = 14.0
    assert last_set_value(hass) == 14.0


@pytest.mark.asyncio
async def test_closed_loop_clamped_to_max_current():
    ctrl, hass, clock = make_regulated(last_set=23)
    hass.states.set("sensor.charger_power", 4600)
    set_power(ctrl, hass, -2000)
    clock[0] += 60

    await ctrl._compute_and_apply("timer")

    assert last_set_value(hass) == 24.0


# ---------------------------------------------------------------------------
# Grid power averaging
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_short_spike_is_averaged():
    """10 s spike of +907 W in a 60 s window of −450 W → average ≈ −224 W."""
    ctrl, hass, clock = make_regulated()
    hass.states.set("sensor.grid_power", -450)
    ctrl._reset_power_average()

    clock[0] += 50
    set_power(ctrl, hass, 907)
    clock[0] += 10
    set_power(ctrl, hass, -450)

    average = ctrl._read_power_w()

    assert average == pytest.approx((50 * -450 + 10 * 907) / 60)


@pytest.mark.asyncio
async def test_spike_at_tick_no_longer_drops_current():
    """Live case 11:08: one sample of +907 W import made 14 A → 8 A. Averaged, it barely moves."""
    ctrl, hass, clock = make_regulated(last_set=14)
    hass.states.set("sensor.charger_power", 2800)
    hass.states.set("sensor.grid_power", -100)       # converged: ~100 W export
    ctrl._reset_power_average()

    clock[0] += 55
    set_power(ctrl, hass, 907)                       # spike right before the tick
    clock[0] += 5

    await ctrl._compute_and_apply("timer")

    assert ctrl._last_set_current >= 13


@pytest.mark.asyncio
async def test_sustained_load_is_fully_seen():
    ctrl, hass, clock = make_regulated(last_set=14)
    hass.states.set("sensor.charger_power", 2800)
    hass.states.set("sensor.grid_power", -100)
    ctrl._reset_power_average()

    set_power(ctrl, hass, 600)                       # oven on for the whole interval
    clock[0] += 60
    await ctrl._compute_and_apply("timer")

    # (−600 − 30) / 230 = −2.7 A → 14 − 3 = 11 A
    assert last_set_value(hass) == 11.0


@pytest.mark.asyncio
async def test_unavailable_power_skips_tick_even_with_average():
    ctrl, hass, clock = make_regulated(last_set=14)
    hass.states.set("sensor.grid_power", -450)
    ctrl._reset_power_average()
    clock[0] += 30
    set_power(ctrl, hass, "unavailable")
    clock[0] += 30

    await ctrl._compute_and_apply("timer")

    assert hass.services.calls == []


@pytest.mark.asyncio
async def test_timer_start_resets_average_window():
    ctrl, hass, clock = make_regulated()
    ctrl._avg_integral = 1e9                         # stale data from an idle period
    ctrl._avg_weight_s = 1.0
    hass.states.set("sensor.grid_power", -450)

    ctrl._start_timer()

    assert ctrl._avg_integral == 0.0
    assert ctrl._avg_last_value == -450.0


@pytest.mark.asyncio
async def test_power_listener_removed_on_stop():
    ctrl, hass, clock = make_regulated()
    await ctrl.async_start()
    unsub = ctrl._unsub_power_listener
    assert unsub is not None

    await ctrl.async_stop()

    assert ctrl._unsub_power_listener is None
    assert unsub.called   # stub returns one shared mock for both listeners


# ---------------------------------------------------------------------------
# Race: charger reports stopped_state while the stop press is still awaited
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_update_during_stop_press_arms_recovery():
    states = {
        "sensor.grid_power": 200,
        "sensor.grid_voltage": VOLTAGE,
        "sensor.charger_status": "Charging",
    }
    ctrl, hass, _ = make_controller(states)
    ctrl._is_charging = True

    def charger_reacts(domain, service, data):
        if service == "press":
            hass.states.set("sensor.charger_status", "Stopped")
            ctrl._handle_charger_status_change(types.SimpleNamespace(data={
                "old_state": FakeState("Charging"),
                "new_state": FakeState("Stopped"),
            }))

    hass.services.on_call = charger_reacts

    await ctrl._compute_and_apply("timer")

    assert ctrl._stopped_by_us is True
    assert ctrl._unsub_recovery_timer is not None, "Recovery must be armed despite the race"
    assert hass.services.count("set_value") == 0, "No current write after the charger stopped"
