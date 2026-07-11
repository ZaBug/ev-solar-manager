"""Unit tests for the minimum-surplus threshold logic (v1.2.0 FR).

These tests exercise EVSolarController._compute_and_apply() and
_handle_recovery_timer() in isolation, using a lightweight fake HomeAssistant
stub – no real HA instance is needed.

Scenario under test
-------------------
The controller must stop the charger (press the start/stop button) whenever:

    available_w < min_current × voltage × phases

i.e. even when the grid meter shows *some* solar injection, if that injection
is less than the minimum viable charging current in watts the charger would
draw the remainder from the grid.  The recovery timer must restart charging
only once the surplus reaches or exceeds this threshold.

Run with:
    python -m pytest tests/test_min_surplus_threshold.py -v
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from tests.conftest import make_controller


# ---------------------------------------------------------------------------
# Tests: _compute_and_apply threshold behaviour
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_when_surplus_below_min_threshold():
    """available_w < min_current×V×phases → button pressed, _stopped_by_us=True."""
    # 230 V × 6 A × 1 phase = 1380 W threshold
    # Export = 500 W (positive solar but < 1380 W) → should stop
    states = {
        "sensor.grid_power": -500,   # export_is_negative=True → export = +500 W
        "sensor.grid_voltage": 230,
    }
    ctrl, hass, _ = make_controller(states)
    ctrl._is_charging = True

    await ctrl._compute_and_apply("timer")

    button_calls = [c for c in hass.services.calls if c["service"] == "press"]
    assert len(button_calls) == 1, "Expected one button press (stop)"
    assert ctrl._stopped_by_us is True


@pytest.mark.asyncio
async def test_no_stop_when_surplus_exactly_at_threshold():
    """available_w == min_current×V×phases → charger runs at min_current, no stop."""
    # 230 × 6 × 1 = 1380 W exactly
    states = {
        "sensor.grid_power": -1380,
        "sensor.grid_voltage": 230,
    }
    ctrl, hass, _ = make_controller(states)
    ctrl._is_charging = True

    await ctrl._compute_and_apply("timer")

    button_calls = [c for c in hass.services.calls if c["service"] == "press"]
    assert len(button_calls) == 0, "No stop expected when surplus == threshold"
    number_calls = [c for c in hass.services.calls if c["service"] == "set_value"]
    assert len(number_calls) == 1
    assert number_calls[0]["data"]["value"] == 6.0


@pytest.mark.asyncio
async def test_no_stop_when_surplus_above_threshold():
    """available_w >> threshold → charger is set to a higher current, no stop."""
    # 2300 W available → 10 A
    states = {
        "sensor.grid_power": -2300,
        "sensor.grid_voltage": 230,
    }
    ctrl, hass, _ = make_controller(states)
    ctrl._is_charging = True

    await ctrl._compute_and_apply("timer")

    button_calls = [c for c in hass.services.calls if c["service"] == "press"]
    assert len(button_calls) == 0
    number_calls = [c for c in hass.services.calls if c["service"] == "set_value"]
    assert len(number_calls) == 1
    assert number_calls[0]["data"]["value"] == 10.0


@pytest.mark.asyncio
async def test_stop_when_drawing_from_grid():
    """available_w < 0 (importing from grid) → button pressed."""
    states = {
        "sensor.grid_power": 200,   # importing 200 W from grid
        "sensor.grid_voltage": 230,
    }
    ctrl, hass, _ = make_controller(states)
    ctrl._is_charging = True

    await ctrl._compute_and_apply("timer")

    button_calls = [c for c in hass.services.calls if c["service"] == "press"]
    assert len(button_calls) == 1
    assert ctrl._stopped_by_us is True


@pytest.mark.asyncio
async def test_already_stopped_does_not_double_press():
    """If _stopped_by_us is already True, the button is NOT pressed again."""
    states = {
        "sensor.grid_power": -100,  # only 100 W surplus, well below 1380 W threshold
        "sensor.grid_voltage": 230,
    }
    ctrl, hass, _ = make_controller(states)
    ctrl._is_charging = True
    ctrl._stopped_by_us = True   # already stopped by us

    await ctrl._compute_and_apply("timer")

    button_calls = [c for c in hass.services.calls if c["service"] == "press"]
    assert len(button_calls) == 0, "Button must not be pressed twice"


@pytest.mark.asyncio
async def test_three_phase_threshold():
    """Three-phase: threshold = min_current × voltage × 3."""
    # threshold = 6 × 230 × 3 = 4140 W
    # surplus = 3000 W → below threshold → stop
    states = {
        "sensor.grid_power": -3000,
        "sensor.grid_voltage": 230,
    }
    ctrl, hass, _ = make_controller(states, phases=3)
    ctrl._is_charging = True

    await ctrl._compute_and_apply("timer")

    button_calls = [c for c in hass.services.calls if c["service"] == "press"]
    assert len(button_calls) == 1, "3-phase: 3000 W < 4140 W threshold → should stop"


@pytest.mark.asyncio
async def test_three_phase_above_threshold():
    """Three-phase: surplus 5000 W > 4140 W threshold → no stop, correct current set."""
    # 5000 W / (230 × 3) = 7.25 A → round to 7 A → clamped to [6, 24] → 7 A
    states = {
        "sensor.grid_power": -5000,
        "sensor.grid_voltage": 230,
    }
    ctrl, hass, _ = make_controller(states, phases=3)
    ctrl._is_charging = True

    await ctrl._compute_and_apply("timer")

    button_calls = [c for c in hass.services.calls if c["service"] == "press"]
    assert len(button_calls) == 0
    number_calls = [c for c in hass.services.calls if c["service"] == "set_value"]
    assert len(number_calls) == 1
    assert number_calls[0]["data"]["value"] == 7.0


@pytest.mark.asyncio
async def test_stop_on_no_injection_disabled_keeps_min_current():
    """When stop_on_no_injection=False, controller falls back to min_current even below threshold."""
    states = {
        "sensor.grid_power": -500,
        "sensor.grid_voltage": 230,
    }
    ctrl, hass, _ = make_controller(states, stop_on_no_injection=False)
    ctrl._is_charging = True

    await ctrl._compute_and_apply("timer")

    button_calls = [c for c in hass.services.calls if c["service"] == "press"]
    assert len(button_calls) == 0, "No stop expected when stop_on_no_injection=False"
    number_calls = [c for c in hass.services.calls if c["service"] == "set_value"]
    assert len(number_calls) == 1
    assert number_calls[0]["data"]["value"] == 6.0  # min_current fallback


# ---------------------------------------------------------------------------
# Tests: _handle_recovery_timer threshold behaviour
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recovery_timer_restarts_when_surplus_sufficient():
    """Recovery timer presses start when surplus >= min_current×V×phases."""
    # 1500 W > 1380 W → restart
    states = {
        "sensor.grid_power": -1500,
        "sensor.grid_voltage": 230,
        "sensor.charger_status": "Stopped",
    }
    ctrl, hass, _ = make_controller(states)
    ctrl._stopped_by_us = True
    ctrl._unsub_recovery_timer = MagicMock()

    await ctrl._handle_recovery_timer(None)

    button_calls = [c for c in hass.services.calls if c["service"] == "press"]
    assert len(button_calls) == 1, "Start button should be pressed when surplus returns"


@pytest.mark.asyncio
async def test_recovery_timer_waits_when_surplus_still_low():
    """Recovery timer does NOT press start when surplus < threshold."""
    # 800 W < 1380 W → keep waiting
    states = {
        "sensor.grid_power": -800,
        "sensor.grid_voltage": 230,
        "sensor.charger_status": "Stopped",
    }
    ctrl, hass, _ = make_controller(states)
    ctrl._stopped_by_us = True
    ctrl._unsub_recovery_timer = MagicMock()

    await ctrl._handle_recovery_timer(None)

    button_calls = [c for c in hass.services.calls if c["service"] == "press"]
    assert len(button_calls) == 0, "Start button must not be pressed while surplus is still below threshold"


@pytest.mark.asyncio
async def test_recovery_timer_waits_when_still_importing():
    """Recovery timer does NOT press start when grid is being imported."""
    states = {
        "sensor.grid_power": 300,   # importing 300 W
        "sensor.grid_voltage": 230,
        "sensor.charger_status": "Stopped",
    }
    ctrl, hass, _ = make_controller(states)
    ctrl._stopped_by_us = True
    ctrl._unsub_recovery_timer = MagicMock()

    await ctrl._handle_recovery_timer(None)

    button_calls = [c for c in hass.services.calls if c["service"] == "press"]
    assert len(button_calls) == 0
