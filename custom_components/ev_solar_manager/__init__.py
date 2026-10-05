"""EV Solar Manager – Home Assistant custom component.

Automatically adjusts the EV charger current to consume only the solar
surplus exported to the grid, with an optional manual override.

The controller is event-driven: it watches the charger status sensor and
starts/stops the periodic recalculation timer based on whether the charger
is actively charging. This avoids unnecessary API calls and log noise when
the car is not connected or has finished charging.

Minimal YAML configuration example:

ev_solar_manager:
  power_entity: sensor.principal_power      # grid power sensor (negative = exporting)
  voltage_entity: sensor.principal_voltage  # grid voltage sensor (V)
  target_number: number.duosida_set_maximal_current

Full configuration example:

ev_solar_manager:
  power_entity: sensor.principal_power
  voltage_entity: sensor.principal_voltage
  target_number: number.duosida_set_maximal_current
  min_current: 6              # minimum charging current in Amperes (default 6)
  max_current: 24             # maximum charging current in Amperes (default 24)
  update_interval: 60         # how often to recalculate, in seconds (default 60)
  min_delta_amp: 1            # minimum change in Amperes before writing to charger (default 1)
  export_is_negative: true    # true if the power sensor is negative when exporting (default true)
  phases: 1                   # number of charging phases: 1 or 3 (default 1)
  charger_power_entity: sensor.shellyem3_xxxx_channel_b_power  # optional: real charger power (W)
  safety_margin_w: 100        # optional: keep this many Watts as buffer (default 0)
  charger_status_entity: sensor.duosida_status   # optional: charger status sensor
  charging_state: "Charging"                     # optional: state value that means charging (default "Charging")
  charger_start_stop_button: button.duosida_start_stop_charging  # optional: toggle button (requires charger_status_entity)
  stopped_state: "Stopped"                       # optional: state value that means stopped/waiting (default "Stopped")
  start_hysteresis_w: 200     # optional: extra surplus needed to restart after a stop (default 200)
  stop_delay_s: 120           # optional: surplus must stay too low this long before stopping (default 120)
  start_delay_s: 120          # optional: surplus must stay high enough this long before restarting (default 120)
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import timedelta

import voluptuous as vol

from homeassistant.core import HomeAssistant, callback
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers.event import async_track_time_interval, async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.config_entries import ConfigEntry

from .const import (
    DOMAIN,
    CONF_POWER_ENTITY,
    CONF_VOLTAGE_ENTITY,
    CONF_TARGET_NUMBER,
    CONF_MIN_CURRENT,
    CONF_MAX_CURRENT,
    CONF_UPDATE_INTERVAL,
    CONF_MIN_DELTA_AMP,
    CONF_EXPORT_IS_NEGATIVE,
    CONF_PHASES,
    CONF_CHARGER_POWER_ENTITY,
    CONF_SAFETY_MARGIN_W,
    CONF_CHARGER_STATUS_ENTITY,
    CONF_CHARGING_STATE,
    CONF_CHARGER_START_STOP_BUTTON,
    CONF_STOPPED_STATE,
    CONF_START_HYSTERESIS_W,
    CONF_STOP_DELAY_S,
    CONF_START_DELAY_S,
    DEFAULT_MIN_CURRENT,
    DEFAULT_MAX_CURRENT,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_MIN_DELTA_AMP,
    DEFAULT_EXPORT_IS_NEGATIVE,
    DEFAULT_PHASES,
    DEFAULT_SAFETY_MARGIN_W,
    DEFAULT_CHARGING_STATE,
    DEFAULT_STOPPED_STATE,
    DEFAULT_START_HYSTERESIS_W,
    DEFAULT_STOP_DELAY_S,
    DEFAULT_START_DELAY_S,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["switch", "number", "sensor", "button"]

# Explicit user actions / state transitions: always write, ignore min_delta_amp.
_BYPASS_DELTA: frozenset[str] = frozenset({
    "startup",
    "charging_started",
    "stop_on_no_injection_toggle",
    "manual_trigger",
    "override_toggle",
    "override_value",
})

# Transient states reported by flaky (cloud) integrations – never treated as a real transition.
_UNAVAILABLE_STATES: frozenset[str] = frozenset({"unavailable", "unknown"})

_FALLBACK_VOLTAGE_V = 230.0

# The charger button is a toggle: pressing it again too early (while the charger is still
# reporting the previous state) would undo the first press. Retries are therefore slow and few.
_PRESS_RETRY_COOLDOWN_S = 300
_MAX_PRESS_ATTEMPTS = 3

# Closed-loop regulation on the grid meter: ignore errors smaller than this (suppresses
# 11↔12 A flapping around a rounding boundary) and limit each correction step.
# Asymmetric: grid import is corrected sooner than surplus export.
_DEADBAND_IMPORT_A = 0.3
_DEADBAND_EXPORT_A = 0.6
_MAX_STEP_A = 3

# Anti-windup (needs charger_power_entity): when the car limits the current itself
# (taper, battery temperature), the setpoint must not climb far above what the charger
# really draws. Increases are capped at measured_amps / ratio + headroom.
_CHARGER_DRAW_RATIO = 0.85
_WINDUP_HEADROOM_A = 2

# First tick without a previous write (e.g. after an HA restart while charging): start the
# closed loop from the charger's current setpoint, but only if the measured draw confirms
# the charger follows it – a stale/high setpoint (manual 32 A, car tapering) would
# otherwise take minutes at -3 A/tick to come down while importing.
_SEED_MIN_DRAW_RATIO = 0.7
_SEED_MAX_DRAW_RATIO = 1.15   # drawing clearly more than the setpoint → setpoint is stale

# Persisted controller state (survives HA restarts)
# The YAML block is passed through unchanged and stored in the config entry;
# option validation and defaults are applied in async_setup_entry.
CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.Schema({}, extra=vol.ALLOW_EXTRA)}, extra=vol.ALLOW_EXTRA
)

_STORAGE_VERSION = 1
_STORAGE_KEY = f"{DOMAIN}.state"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Handle YAML configuration – trigger config flow import or update existing entry.

    Every time HA loads (or reloads) the integration, we push the current YAML
    values into the config entry so that changes to configuration.yaml are
    picked up without having to delete and recreate the entry.
    """
    if DOMAIN not in config:
        return True

    yaml_data = dict(config[DOMAIN])
    existing_entries = hass.config_entries.async_entries(DOMAIN)

    if not existing_entries:
        # First run – create the config entry via the import flow.
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": "import"},
                data=yaml_data,
            )
        )
    else:
        # Entry already exists – update its data with the current YAML values
        # so changes to configuration.yaml take effect on the next HA reload.
        entry = existing_entries[0]
        if entry.data != yaml_data:
            _LOGGER.info(
                "EV Solar Manager: configuration.yaml changed – updating config entry and reloading"
            )
            hass.config_entries.async_update_entry(entry, data=yaml_data)
            hass.async_create_task(
                hass.config_entries.async_reload(entry.entry_id)
            )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up EV Solar Manager from a config entry (created via YAML import)."""
    cfg = dict(entry.data)

    power_entity = cfg.get(CONF_POWER_ENTITY)
    voltage_entity = cfg.get(CONF_VOLTAGE_ENTITY)
    target_number = cfg.get(CONF_TARGET_NUMBER)

    if not power_entity or not voltage_entity or not target_number:
        _LOGGER.error(
            "Missing required configuration: power_entity, voltage_entity, target_number"
        )
        return False

    min_current = int(cfg.get(CONF_MIN_CURRENT, DEFAULT_MIN_CURRENT))
    max_current = int(cfg.get(CONF_MAX_CURRENT, DEFAULT_MAX_CURRENT))
    update_interval = int(cfg.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL))
    min_delta_amp = int(cfg.get(CONF_MIN_DELTA_AMP, DEFAULT_MIN_DELTA_AMP))
    export_is_negative = bool(cfg.get(CONF_EXPORT_IS_NEGATIVE, DEFAULT_EXPORT_IS_NEGATIVE))
    phases = int(cfg.get(CONF_PHASES, DEFAULT_PHASES))
    charger_power_entity = cfg.get(CONF_CHARGER_POWER_ENTITY)
    safety_margin_w = float(cfg.get(CONF_SAFETY_MARGIN_W, DEFAULT_SAFETY_MARGIN_W))
    charger_status_entity = cfg.get(CONF_CHARGER_STATUS_ENTITY)
    charging_state = cfg.get(CONF_CHARGING_STATE, DEFAULT_CHARGING_STATE)
    charger_start_stop_button = cfg.get(CONF_CHARGER_START_STOP_BUTTON)
    stopped_state = cfg.get(CONF_STOPPED_STATE, DEFAULT_STOPPED_STATE)
    start_hysteresis_w = float(cfg.get(CONF_START_HYSTERESIS_W, DEFAULT_START_HYSTERESIS_W))
    stop_delay_s = float(cfg.get(CONF_STOP_DELAY_S, DEFAULT_STOP_DELAY_S))
    start_delay_s = float(cfg.get(CONF_START_DELAY_S, DEFAULT_START_DELAY_S))

    hass.data.setdefault(DOMAIN, {})
    _LOGGER.info(
        "EV Solar Manager: initializing – power_entity=%s voltage_entity=%s "
        "target_number=%s min_current=%s max_current=%s update_interval=%ss "
        "min_delta_amp=%s export_is_negative=%s phases=%s "
        "charger_power_entity=%s safety_margin_w=%s "
        "charger_status_entity=%s charging_state=%s "
        "charger_start_stop_button=%s stopped_state=%s "
        "start_hysteresis_w=%s stop_delay_s=%s start_delay_s=%s",
        power_entity, voltage_entity, target_number,
        min_current, max_current, update_interval,
        min_delta_amp, export_is_negative, phases,
        charger_power_entity, safety_margin_w,
        charger_status_entity, charging_state,
        charger_start_stop_button, stopped_state,
        start_hysteresis_w, stop_delay_s, start_delay_s,
    )

    controller = EVSolarController(
        hass=hass,
        power_entity=power_entity,
        voltage_entity=voltage_entity,
        target_number=target_number,
        min_current=min_current,
        max_current=max_current,
        min_delta_amp=min_delta_amp,
        update_interval=update_interval,
        export_is_negative=export_is_negative,
        phases=phases,
        charger_power_entity=charger_power_entity,
        safety_margin_w=safety_margin_w,
        charger_status_entity=charger_status_entity,
        charging_state=charging_state,
        charger_start_stop_button=charger_start_stop_button,
        stopped_state=stopped_state,
        start_hysteresis_w=start_hysteresis_w,
        stop_delay_s=stop_delay_s,
        start_delay_s=start_delay_s,
    )
    hass.data[DOMAIN]["controller"] = controller

    await controller.async_start()
    _LOGGER.info("EV Solar Manager: controller started")

    async def _handle_stop(_event):
        await controller.async_stop()

    hass.data[DOMAIN]["cancel_stop"] = hass.bus.async_listen_once(
        EVENT_HOMEASSISTANT_STOP, _handle_stop
    )

    # Load platforms – they will pick up device_info from the config entry
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    domain_data = hass.data.get(DOMAIN, {})

    cancel_stop = domain_data.get("cancel_stop")
    if cancel_stop:
        cancel_stop()

    controller = domain_data.get("controller")
    if controller:
        await controller.async_stop()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.pop(DOMAIN, None)
    return unload_ok


class EVSolarController:
    """Compute the target EV charging current from solar export power and voltage.

    State machine
    -------------
    If charger_status_entity is configured:

      charger → charging_state (e.g. "Charging")
          └─► _start_timer()  – periodic recalculation every update_interval seconds
              └─► each tick: average grid power over the interval, correct the last
                  current by the remaining export (closed loop), write to charger
              └─► surplus below min_surplus_w for stop_delay_s: press stop (held at
                  min_current while waiting)

      charger → stopped_state (e.g. "Stopped") AND _stopped_by_us is True
      (_stopped_by_us is persisted, so this also works after an HA restart)
          └─► _start_recovery_timer()  – checks every update_interval if solar returned
              └─► surplus ≥ min_surplus_w + start_hysteresis_w for start_delay_s:
                  press start → charger resumes → _start_timer()

      charger → unavailable / unknown
          └─► ignored – transient glitch, state is kept

      charger → any other state (Finished / Available / disconnected / etc.)
          └─► _stop_timer() + _stop_recovery_timer()  – no more API calls

    If charger_status_entity is NOT configured:
      Falls back to always-on timer (original behaviour). The start/stop button is
      ignored, because pressing a toggle without status feedback is unsafe.

    If charger_start_stop_button is NOT configured:
      Falls back to keeping min_current instead of pressing stop.

    Override mode bypasses the charging state check and stop-on-no-injection entirely,
    and restarts the charger if we had stopped it.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        power_entity: str,
        voltage_entity: str,
        target_number: str,
        min_current: int,
        max_current: int,
        min_delta_amp: int,
        update_interval: int,
        export_is_negative: bool = True,
        phases: int = 1,
        charger_power_entity: str | None = None,
        safety_margin_w: float = 0.0,
        charger_status_entity: str | None = None,
        charging_state: str = "Charging",
        charger_start_stop_button: str | None = None,
        stopped_state: str = "Stopped",
        start_hysteresis_w: float = 0.0,
        stop_delay_s: float = 0.0,
        start_delay_s: float = 0.0,
    ) -> None:
        if charger_start_stop_button and not charger_status_entity:
            _LOGGER.warning(
                "EV Solar Manager: charger_start_stop_button is configured without "
                "charger_status_entity – the button is ignored, because pressing a toggle "
                "without status feedback could start the charger instead of stopping it. "
                "Add charger_status_entity to enable automatic stop/start."
            )
            charger_start_stop_button = None

        self.hass = hass
        self.power_entity = power_entity
        self.voltage_entity = voltage_entity
        self.target_number = target_number
        self.min_current = min_current
        self.max_current = max_current
        self.min_delta_amp = min_delta_amp
        self.update_interval = update_interval
        self.export_is_negative = export_is_negative
        self.phases = phases
        self.charger_power_entity = charger_power_entity
        self.safety_margin_w = safety_margin_w
        self.charger_status_entity = charger_status_entity
        self.charging_state = charging_state
        self.charger_start_stop_button = charger_start_stop_button
        self.stopped_state = stopped_state
        self.start_hysteresis_w = start_hysteresis_w
        self.stop_delay_s = stop_delay_s
        self.start_delay_s = start_delay_s

        self._unsub_timer = None
        self._unsub_recovery_timer = None
        self._unsub_status_listener = None
        self._unsub_power_listener = None
        self._startup_task: asyncio.Task | None = None
        self._store = Store(hass, _STORAGE_VERSION, _STORAGE_KEY)
        self._lock = asyncio.Lock()
        self._last_set_current: int | None = None
        self._override_enabled: bool = False
        self._override_current: int = min_current
        self._computed_current: int = 0
        self._sensor_entity = None
        self._is_charging: bool = False   # charger is in charging_state
        self._last_real_status: str | None = None  # last charger status that was not unavailable/unknown
        self._stop_on_no_injection: bool = True   # stop charger when no solar surplus
        self._stopped_by_us_value: bool = False    # see _stopped_by_us property (persisted)
        self._available: bool = False

        # Anti-flapping / button retry bookkeeping (monotonic timestamps)
        self._low_surplus_since: float | None = None
        self._high_surplus_since: float | None = None
        self._press_attempts: int = 0
        self._last_press_at: float = float("-inf")
        self._last_write_at: float = float("-inf")   # last number.set_value we issued

        # Time-weighted average of power_entity between two reads (filters short load spikes)
        self._avg_integral: float = 0.0
        self._avg_weight_s: float = 0.0
        self._avg_last_value: float | None = None
        self._avg_last_ts: float | None = None

    @property
    def _stopped_by_us(self) -> bool:
        """True when we pressed stop due to no surplus. Persisted so a restart can tell
        our stop apart from a full car or a manual stop."""
        return self._stopped_by_us_value

    @_stopped_by_us.setter
    def _stopped_by_us(self, value: bool) -> None:
        if value != self._stopped_by_us_value:
            self._stopped_by_us_value = value
            self._store.async_delay_save(self._state_to_save, 1)

    def _state_to_save(self) -> dict:
        return {"stopped_by_us": self._stopped_by_us_value}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_start(self) -> None:
        """Start the controller.

        If charger_status_entity is set:
          - Register a state change listener on it
          - Wait briefly for entities to become available after HA startup,
            then check the current charger state
        Else:
          - Start the timer unconditionally (legacy behaviour)
        """
        self._available = True

        stored = await self._store.async_load()
        if stored:
            self._stopped_by_us_value = bool(stored.get("stopped_by_us", False))

        # Feed the time-weighted grid power average
        self._unsub_power_listener = async_track_state_change_event(
            self.hass,
            self.power_entity,
            self._handle_power_change,
        )

        if self.charger_status_entity:
            # Watch for charging state transitions
            self._unsub_status_listener = async_track_state_change_event(
                self.hass,
                self.charger_status_entity,
                self._handle_charger_status_change,
            )
            # Delay the startup check slightly so integrations (Duosida) have time
            # to report their real state instead of 'unavailable'.
            self._startup_task = self.hass.async_create_task(self._delayed_startup_check())
        else:
            # No status entity configured → always-on timer
            _LOGGER.info(
                "EV Solar Manager: no charger_status_entity configured – running in always-on mode"
            )
            self._is_charging = True
            self._start_timer()
            self.hass.async_create_task(self._compute_and_apply("startup"))

        if self._stop_on_no_injection and not self.charger_start_stop_button:
            _LOGGER.warning(
                "EV Solar Manager: switch.ev_solar_manager_stop_when_no_solar_surplus is enabled "
                "but charger_start_stop_button is not configured – the switch has no effect. "
                "Add charger_start_stop_button to your configuration to enable automatic stop/start."
            )

    async def _delayed_startup_check(self) -> None:
        """Wait for integrations to settle, then check charger state.

        Three outcomes after the delay:
          1. charging_state  → start the recalculation timer immediately.
          2. stopped_state + persisted _stopped_by_us + stop_on_no_injection + button
                             → arm the recovery timer so charging resumes
                               automatically once solar surplus is sufficient
                               (we had stopped it before the restart).
          3. Anything else (stopped by the user, car full, disconnected, …) → stay idle.
        """
        await asyncio.sleep(10)  # give Duosida / other integrations 10s to report real state
        if not self._available:
            return  # controller was stopped/unloaded while we were waiting
        state_val = self._charger_status()
        _LOGGER.info(
            "EV Solar Manager: startup check – charger status is '%s'", state_val
        )
        if state_val not in _UNAVAILABLE_STATES:
            if state_val == self._last_real_status:
                # The status listener already handled this state during the delay
                # (e.g. unavailable → Charging while integrations were loading).
                _LOGGER.debug(
                    "EV Solar Manager: startup check – '%s' already handled by the status listener",
                    state_val,
                )
                return
            self._last_real_status = state_val

        if state_val == self.charging_state:
            _LOGGER.info(
                "EV Solar Manager: charger is %s at startup – starting timer",
                self.charging_state,
            )
            self._is_charging = True
            self._stopped_by_us = False
            self._start_timer()
            await self._compute_and_apply("startup")

        elif (
            state_val == self.stopped_state
            and self._stopped_by_us
            and self._stop_on_no_injection
            and self.charger_start_stop_button
        ):
            # We had stopped the charger for lack of surplus before the restart.
            # Arm the recovery timer – it will press start as soon as surplus
            # is sufficient, without waiting for user intervention.
            self._start_recovery_timer()
            _LOGGER.info(
                "EV Solar Manager: charger is '%s' at startup and was stopped by us – arming "
                "recovery timer to restart when solar surplus is sufficient",
                state_val,
            )

        elif state_val in _UNAVAILABLE_STATES:
            # Keep the persisted flag; the status listener re-arms recovery once the
            # charger reports stopped_state again.
            _LOGGER.info(
                "EV Solar Manager: charger status '%s' at startup – waiting for the status listener",
                state_val,
            )

        else:
            # Stopped by the user, car full, disconnected, … – not ours to restart
            self._stopped_by_us = False
            _LOGGER.info(
                "EV Solar Manager: charger status '%s' – staying idle at startup",
                state_val,
            )

    async def async_stop(self) -> None:
        """Cancel timers, pending tasks and all listeners on shutdown."""
        self._available = False
        if self._startup_task is not None and not self._startup_task.done():
            self._startup_task.cancel()
        self._startup_task = None
        self._stop_timer()
        self._stop_recovery_timer()
        if self._unsub_status_listener:
            self._unsub_status_listener()
            self._unsub_status_listener = None
        if self._unsub_power_listener:
            self._unsub_power_listener()
            self._unsub_power_listener = None

    async def async_force_recalculate(self) -> None:
        """Trigger an immediate recalculation (called by the Recalculate Now button)."""
        await self._compute_and_apply("manual_trigger")

    # ------------------------------------------------------------------
    # Timer management
    # ------------------------------------------------------------------

    def _start_timer(self) -> None:
        """Start the periodic recalculation timer (idempotent, no-op once stopped)."""
        if self._unsub_timer is None and self._available:
            self._unsub_timer = async_track_time_interval(
                self.hass, self._handle_timer, timedelta(seconds=self.update_interval)
            )
            self._reset_power_average()
            _LOGGER.debug("EV Solar Manager: recalculation timer started")

    def _stop_timer(self) -> None:
        """Stop the periodic recalculation timer (idempotent)."""
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None
            _LOGGER.debug("EV Solar Manager: recalculation timer stopped")

    def _start_recovery_timer(self) -> None:
        """Start the solar-return recovery timer (idempotent, no-op once stopped)."""
        if self._unsub_recovery_timer is None and self._available:
            self._unsub_recovery_timer = async_track_time_interval(
                self.hass, self._handle_recovery_timer, timedelta(seconds=self.update_interval)
            )
            self._reset_power_average()
            _LOGGER.debug("EV Solar Manager: solar recovery timer started")

    def _stop_recovery_timer(self) -> None:
        """Stop the solar-return recovery timer (idempotent)."""
        if self._unsub_recovery_timer is not None:
            self._unsub_recovery_timer()
            self._unsub_recovery_timer = None
            _LOGGER.debug("EV Solar Manager: solar recovery timer stopped")

    def _reset_press_tracking(self) -> None:
        """Forget anti-flapping timers and button retry state."""
        self._low_surplus_since = None
        self._high_surplus_since = None
        self._press_attempts = 0
        self._last_press_at = float("-inf")

    def _can_press(self, now: float) -> bool:
        """Return True if another button press is allowed (attempt limit + cooldown)."""
        return (
            self._press_attempts < _MAX_PRESS_ATTEMPTS
            and now - self._last_press_at >= _PRESS_RETRY_COOLDOWN_S
        )

    @staticmethod
    def _monotonic() -> float:
        return time.monotonic()

    # ------------------------------------------------------------------
    # State change listener – charger status
    # ------------------------------------------------------------------

    @callback
    def _handle_charger_status_change(self, event) -> None:
        """React to charger status state changes."""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        new_val = new_state.state if new_state else "unavailable"
        old_val = old_state.state if old_state else "unavailable"

        if new_val == old_val:
            return  # no actual change

        _LOGGER.info(
            "EV Solar Manager: charger status changed '%s' → '%s'",
            old_val, new_val,
        )

        if new_val in _UNAVAILABLE_STATES:
            # Cloud glitch – keep the current state (especially _stopped_by_us) so a
            # Stopped → unavailable → Stopped blip does not lose the recovery.
            _LOGGER.debug(
                "EV Solar Manager: ignoring transient charger status '%s'", new_val
            )
            return

        if new_val == self._last_real_status:
            # X → unavailable → X: nothing really changed. Re-running the transition would
            # reset the stop/start delays and force a write on every cloud blip.
            _LOGGER.debug(
                "EV Solar Manager: charger status back to '%s' after a glitch – no change", new_val
            )
            return
        self._last_real_status = new_val

        if new_val == self.charging_state:
            # Charger started charging → start recalculation timer
            self._is_charging = True
            self._stopped_by_us = False
            self._reset_press_tracking()
            self._stop_recovery_timer()
            self._start_timer()
            self.hass.async_create_task(self._compute_and_apply("charging_started"))

        elif new_val == self.stopped_state and self._stopped_by_us:
            # We pressed stop due to no surplus → watch for solar to return
            self._is_charging = False
            self._stop_timer()
            self._last_set_current = None
            self._reset_press_tracking()
            self._set_computed_current(0)
            self._start_recovery_timer()
            _LOGGER.info(
                "EV Solar Manager: charger stopped by us – waiting for solar surplus to return"
            )

        else:
            # Car disconnected / charging finished / user stopped manually → reset everything
            self._is_charging = False
            self._stopped_by_us = False
            self._stop_timer()
            self._stop_recovery_timer()
            self._last_set_current = None
            self._reset_press_tracking()
            self._set_computed_current(0)
            _LOGGER.info(
                "EV Solar Manager: charger status '%s' – timers stopped", new_val
            )

    # ------------------------------------------------------------------
    # Timer callbacks
    # ------------------------------------------------------------------

    async def _handle_timer(self, now) -> None:
        """Called every update_interval seconds while charger is actively charging."""
        await self._compute_and_apply("timer")

    async def _handle_recovery_timer(self, now) -> None:
        """Called every update_interval seconds while we wait for solar surplus to return."""
        if not self._available:
            return
        async with self._lock:
            try:
                await self._recovery_tick()
            except Exception as ex:  # pragma: no cover
                _LOGGER.exception("Unexpected error in recovery timer: %s", ex)

    async def _recovery_tick(self) -> None:
        """Press start once the surplus is sufficient and stable, and the charger is still stopped.

        Restart threshold = min_surplus_w + start_hysteresis_w, sustained for start_delay_s.
        The recovery timer keeps running after the press until the charger confirms
        charging_state; if it does not, the press is retried after a cooldown.
        """
        if not self._stopped_by_us or not self.charger_start_stop_button:
            self._stop_recovery_timer()
            return

        # Verify the charger is still in the state we expect (car still connected)
        state_val = self._charger_status()
        if state_val in _UNAVAILABLE_STATES:
            _LOGGER.debug("EV Solar Manager: recovery timer – charger status '%s', retrying", state_val)
            return
        if state_val != self.stopped_state:
            _LOGGER.info(
                "EV Solar Manager: recovery timer – charger is now '%s' (not '%s') – stopping recovery",
                state_val, self.stopped_state,
            )
            self._stopped_by_us = False
            self._reset_press_tracking()
            self._stop_recovery_timer()
            return

        readings = self._read_available_w()
        if readings is None:
            _LOGGER.debug("EV Solar Manager: recovery timer – sensors unavailable, retrying")
            return
        available_w, voltage_v = readings

        start_threshold_w = self.min_current * voltage_v * self.phases + self.start_hysteresis_w
        if available_w < start_threshold_w:
            self._high_surplus_since = None
            _LOGGER.debug(
                "EV Solar Manager: recovery timer – surplus %.1f W still below restart threshold %.1f W – waiting",
                available_w, start_threshold_w,
            )
            return

        now = self._monotonic()
        if self._high_surplus_since is None:
            self._high_surplus_since = now
        sustained_s = now - self._high_surplus_since
        if sustained_s < self.start_delay_s:
            _LOGGER.debug(
                "EV Solar Manager: recovery timer – surplus %.1f W >= %.1f W for %.0fs (need %.0fs) – waiting",
                available_w, start_threshold_w, sustained_s, self.start_delay_s,
            )
            return

        if not self._can_press(now):
            if self._press_attempts >= _MAX_PRESS_ATTEMPTS:
                _LOGGER.error(
                    "EV Solar Manager: charger did not start after %s start presses – giving up, "
                    "start it manually",
                    self._press_attempts,
                )
                self._stopped_by_us = False
                self._reset_press_tracking()
                self._stop_recovery_timer()
            else:
                _LOGGER.debug("EV Solar Manager: recovery timer – start pressed, waiting for charger to confirm")
            return

        if self._press_attempts:
            _LOGGER.warning(
                "EV Solar Manager: charger still '%s' after start press – retrying (attempt %s/%s)",
                state_val, self._press_attempts + 1, _MAX_PRESS_ATTEMPTS,
            )
        else:
            _LOGGER.info(
                "EV Solar Manager: sufficient solar surplus returned (%.1f W >= %.1f W for %.0fs) – pressing start",
                available_w, start_threshold_w, sustained_s,
            )
        self._press_attempts += 1
        self._last_press_at = now
        await self._press_charger_button("surplus_returned_start")

    # ------------------------------------------------------------------
    # Public API (used by switch / number / button entities)
    # ------------------------------------------------------------------

    def set_stop_on_no_injection(self, enabled: bool) -> None:
        """Enable or disable stop-on-no-injection mode.

        If disabled while we had stopped the charger, press start immediately.
        """
        self._stop_on_no_injection = enabled
        if not enabled and self._stopped_by_us:
            # User turned off the feature while charger was stopped by us – resume charging
            _LOGGER.info(
                "EV Solar Manager: stop-on-no-injection disabled – restarting charger we stopped"
            )
            self._resume_charger("stop_on_no_injection_disabled")
        else:
            self.hass.async_create_task(self._compute_and_apply("stop_on_no_injection_toggle"))

    def set_override(self, enabled: bool) -> None:
        """Enable or disable manual override mode.

        Enabling override while we had stopped the charger restarts it.
        """
        self._override_enabled = enabled
        if enabled and self._stopped_by_us:
            _LOGGER.info("EV Solar Manager: override enabled – restarting charger we stopped")
            self._resume_charger("override_enabled")
        self.hass.async_create_task(self._compute_and_apply("override_toggle"))

    def set_override_current(self, amps: int) -> None:
        """Set the manual override current (clamped to min/max)."""
        self._override_current = max(self.min_current, min(self.max_current, int(amps)))
        self.hass.async_create_task(self._compute_and_apply("override_value"))

    @property
    def available(self) -> bool:
        """Return True while the controller is running (False once stopped/unloaded)."""
        return self._available

    @property
    def override_enabled(self) -> bool:
        return self._override_enabled

    @property
    def stop_on_no_injection(self) -> bool:
        return self._stop_on_no_injection

    @property
    def override_current_amps(self) -> int:
        return self._override_current

    def get_computed_current(self) -> int:
        """Return the last computed (solar-based) current in Amperes."""
        return self._computed_current

    def is_charging(self) -> bool:
        """Return True if the charger is currently in the charging state."""
        return self._is_charging

    def register_sensor(self, sensor) -> None:
        """Register the computed-current sensor for push state updates."""
        self._sensor_entity = sensor

    def _push_sensor_state(self) -> None:
        """Push state to the sensor entity, but only if it is fully registered."""
        if self._sensor_entity is not None and self._sensor_entity.entity_id:
            self._sensor_entity.async_write_ha_state()

    def _set_computed_current(self, amps: int) -> None:
        """Update the computed current and push it to the sensor."""
        self._computed_current = amps
        self._push_sensor_state()

    def _resume_charger(self, reason: str) -> None:
        """Clear our stop and press start – only if the charger is really in stopped_state.

        The button is a toggle, so pressing it in any other state could stop a
        charging session instead of starting one.
        """
        self._stopped_by_us = False
        self._stop_recovery_timer()
        self._reset_press_tracking()
        if not self.charger_start_stop_button:
            return
        state_val = self._charger_status()
        if state_val != self.stopped_state:
            _LOGGER.info(
                "EV Solar Manager: not pressing start (%s) – charger is '%s', not '%s'",
                reason, state_val, self.stopped_state,
            )
            return
        self.hass.async_create_task(self._press_charger_button(reason))

    # ------------------------------------------------------------------
    # Core computation
    # ------------------------------------------------------------------

    async def _compute_and_apply(self, reason: str) -> None:
        """Compute the desired charging current and write it to the charger if needed."""
        if not self._available:
            return
        async with self._lock:
            try:
                await self._compute_and_apply_locked(reason)
            except Exception as ex:  # pragma: no cover
                _LOGGER.exception("Unexpected error in _compute_and_apply: %s", ex)

    async def _compute_and_apply_locked(self, reason: str) -> None:
        force = reason in _BYPASS_DELTA

        # --- Guard: the charger's current entity must exist (e.g. not yet loaded after a
        # restart). HA only logs a warning for a missing entity, so a write would be lost
        # while we believed it applied. Retry on the next tick instead.
        target_value = self._read_float(self.target_number)
        if target_value is None:
            _LOGGER.debug(
                "EV Solar Manager: skipping calculation (%s) – %s not available yet",
                reason, self.target_number,
            )
            return

        # --- Override mode: bypasses all charging state and solar checks ---
        if self._override_enabled:
            target = self._override_current
            self._computed_current = target
            await self._maybe_set_current(target, reason + ":override", force)
            self._push_sensor_state()
            return

        # --- Guard: only act when charger is actively charging ---
        # (when charger_status_entity is set; otherwise _is_charging is always True)
        if not self._is_charging:
            _LOGGER.debug(
                "EV Solar Manager: skipping calculation – charger is not in '%s' state",
                self.charging_state,
            )
            return

        # --- Read source entities (grid power averaged over the last interval) ---
        power_w = self._read_power_w()
        voltage_v = self._read_voltage()
        if power_w is None or voltage_v is None:
            _LOGGER.debug("EV Solar Manager: skipping calculation – power or voltage not available")
            return

        # --- Determine net grid power direction ---
        # export_is_negative=True  → sensor is negative when exporting (bidirectional meter)
        # export_is_negative=False → sensor is positive when exporting (production sensor)
        signed_export_w = -power_w if self.export_is_negative else power_w

        self._resync_from_target(target_value)

        # --- Compensate for EV charger load already embedded in the meter reading ---
        # grid_meter = solar - house - ev_charger  (net)
        # available  = grid_meter_export + ev_charger  (gross solar budget)
        # Priority: 1. real charger sensor  2. estimate from last set current
        if self.charger_power_entity:
            charger_consumption_w = self._read_charger_consumption_w()
        elif self._last_set_current is not None:
            charger_consumption_w = self._last_set_current * voltage_v * self.phases
        else:
            charger_consumption_w = 0.0

        available_w = signed_export_w + charger_consumption_w - self.safety_margin_w

        _LOGGER.debug(
            "EV Solar Manager: power_w=%.1f V=%.1f signed_export_w=%.1f "
            "charger_load_w=%.1f safety_margin_w=%.1f available_w=%.1f phases=%s",
            power_w, voltage_v, signed_export_w,
            charger_consumption_w, self.safety_margin_w, available_w, self.phases,
        )

        # --- Guard: insufficient solar surplus ---
        # Stop (or fall back) when available power is less than the minimum needed to
        # sustain IEC 61851 minimum charging current:
        #   min_surplus_w = min_current × voltage × phases
        # This prevents the charger from being set to min_current while actually
        # drawing that deficit power from the grid (e.g. when a washing machine
        # starts and reduces the available solar export below 6 A worth of watts).
        min_surplus_w = self.min_current * voltage_v * self.phases
        if available_w < min_surplus_w:
            await self._handle_low_surplus(available_w, min_surplus_w, reason)
            return

        self._handle_surplus_ok()

        if self._last_set_current is None:
            self._last_set_current = self._confirmed_charger_setpoint(voltage_v * self.phases)

        amps = self._next_current(available_w, signed_export_w, voltage_v)

        self._computed_current = amps
        await self._maybe_set_current(amps, reason, force)
        self._push_sensor_state()

    def _next_current(self, available_w: float, signed_export_w: float, voltage_v: float) -> int:
        """Return the next charging current (A).

        Closed loop on the grid meter: correct the last written current by the remaining
        export (or import) relative to the target export (safety_margin_w):

            step = (signed_export_w − safety_margin_w) / (V × phases)

        This converges even when the charger draws less than its setpoint (e.g. ~87 %),
        which an open-loop I = available / V can never compensate. Errors below
        _DEADBAND_IMPORT_A (import side) / _DEADBAND_EXPORT_A (export side) are ignored,
        import steps are rounded up (export steps are rounded normally),
        each step is limited to ±_MAX_STEP_A and increases are capped by the measured
        charger draw (anti-windup).

        Without a previous write and without a confirmed charger setpoint (see
        _confirmed_charger_setpoint) the open-loop estimate I = available_w / (V × phases)
        is used as the starting point.
        """
        w_per_amp = voltage_v * self.phases
        if self._last_set_current is None:
            amps = round(available_w / w_per_amp)
            return max(self.min_current, min(self.max_current, amps))

        last = self._last_set_current
        step = (signed_export_w - self.safety_margin_w) / w_per_amp
        if step >= _DEADBAND_EXPORT_A:
            delta = min(_MAX_STEP_A, round(step))
        elif step <= -_DEADBAND_IMPORT_A:
            # Import: round the deficit up (−1.48 → −2 A) so grid import is cleared in one tick
            delta = -min(_MAX_STEP_A, math.ceil(-step))
        else:
            delta = 0
        amps = last + delta

        if delta > 0:
            cap = self._windup_cap(w_per_amp)
            if cap is not None and amps > cap:
                _LOGGER.debug(
                    "EV Solar Manager: anti-windup – charger draws less than requested, "
                    "limiting increase to %sA (wanted %sA)", max(last, cap), amps,
                )
                amps = max(last, cap)

        return max(self.min_current, min(self.max_current, amps))

    def _resync_from_target(self, target_value: float) -> None:
        """Adopt the charger's real setpoint if it differs from what we believe we wrote.

        Catches lost writes, charger-side limits (e.g. cable max 16 A while we wrote 24 A)
        and manual changes. Only after one of our own writes, and not right after it, so
        a cloud integration that updates its state a few seconds late is not overridden.
        """
        if self._last_set_current is None or self._last_write_at == float("-inf"):
            return
        if self._monotonic() - self._last_write_at < 0.9 * self.update_interval:
            return
        actual = round(target_value)
        if abs(actual - self._last_set_current) >= 1:
            _LOGGER.info(
                "EV Solar Manager: charger setpoint is %sA, not the %sA we wrote – regulating from %sA",
                actual, self._last_set_current, actual,
            )
            self._last_set_current = actual

    def _confirmed_charger_setpoint(self, w_per_amp: float) -> int | None:
        """Return the charger's current setpoint (target_number) if the charger really follows it.

        Confirmed when the measured draw (charger_power_entity) is between
        _SEED_MIN_DRAW_RATIO and _SEED_MAX_DRAW_RATIO of setpoint × V × phases. None otherwise, or without
        charger_power_entity – the caller then falls back to the open-loop estimate.
        """
        if not self.charger_power_entity:
            return None
        setpoint = self._read_float(self.target_number)
        charger_w = self._read_float(self.charger_power_entity)
        if setpoint is None or charger_w is None or setpoint <= 0:
            _LOGGER.debug(
                "EV Solar Manager: cannot confirm charger setpoint (setpoint=%s, charger_w=%s) – using estimate",
                setpoint, charger_w,
            )
            return None
        setpoint_a = round(setpoint)
        ratio = charger_w / (setpoint_a * w_per_amp)
        if not _SEED_MIN_DRAW_RATIO <= ratio <= _SEED_MAX_DRAW_RATIO:
            _LOGGER.debug(
                "EV Solar Manager: charger setpoint %sA not confirmed (draws %.0f%%) – using estimate",
                setpoint_a, ratio * 100,
            )
            return None
        seed = max(self.min_current, min(self.max_current, setpoint_a))
        _LOGGER.info(
            "EV Solar Manager: starting regulation from the charger setpoint %sA (draws %.0f%%)",
            seed, ratio * 100,
        )
        return seed

    def _windup_cap(self, w_per_amp: float) -> int | None:
        """Highest setpoint allowed for an increase, from the measured charger draw.

        None when charger_power_entity is not configured or not readable.
        """
        if not self.charger_power_entity:
            return None
        charger_w = self._read_float(self.charger_power_entity)
        if charger_w is None:
            return None
        measured_a = max(0.0, charger_w) / w_per_amp
        return round(measured_a / _CHARGER_DRAW_RATIO + _WINDUP_HEADROOM_A)

    async def _handle_low_surplus(self, available_w: float, min_surplus_w: float, reason: str) -> None:
        """Surplus below min_surplus_w: press stop once it stays low for stop_delay_s.

        Until then (or when stopping is not possible / not wanted) the charger is held
        at min_current to limit grid import.
        """
        if self._stop_on_no_injection and self.charger_start_stop_button:
            now = self._monotonic()
            if self._low_surplus_since is None:
                self._low_surplus_since = now
            low_for_s = now - self._low_surplus_since

            if low_for_s < self.stop_delay_s:
                _LOGGER.debug(
                    "EV Solar Manager: surplus too low (available_w=%.1f W < min_surplus_w=%.1f W) "
                    "for %.0fs (stop after %.0fs) – holding min_current",
                    available_w, min_surplus_w, low_for_s, self.stop_delay_s,
                )
            elif self._can_press(now):
                if self._press_attempts:
                    _LOGGER.warning(
                        "EV Solar Manager: charger still '%s' after stop press – retrying (attempt %s/%s)",
                        self.charging_state, self._press_attempts + 1, _MAX_PRESS_ATTEMPTS,
                    )
                else:
                    _LOGGER.info(
                        "EV Solar Manager: surplus too low (available_w=%.1f W < min_surplus_w=%.1f W) "
                        "for %.0fs – pressing stop button",
                        available_w, min_surplus_w, low_for_s,
                    )
                self._press_attempts += 1
                self._last_press_at = now
                # Set the flag BEFORE pressing: the charger integration may refresh its status
                # while the press is awaited, and the status listener needs the flag to arm
                # the recovery timer when it sees stopped_state.
                self._stopped_by_us = True
                if not await self._press_charger_button("no_surplus_stop") and self._is_charging:
                    self._stopped_by_us = False   # press failed and the charger did not stop
            elif self._press_attempts >= _MAX_PRESS_ATTEMPTS:
                _LOGGER.error(
                    "EV Solar Manager: charger did not stop after %s stop presses – holding min_current",
                    self._press_attempts,
                )
            else:
                _LOGGER.debug(
                    "EV Solar Manager: stop pressed, waiting for charger to confirm – holding min_current"
                )

        if not self._is_charging:
            return  # charger already confirmed the stop while the press was awaited

        # Hold min_current (fallback without button, during stop_delay_s, or while waiting for the stop)
        self._computed_current = self.min_current
        if self._last_set_current != self.min_current:
            await self._maybe_set_current(self.min_current, reason, force=True)
        self._push_sensor_state()

    def _handle_surplus_ok(self) -> None:
        """Surplus is back above min_surplus_w while charging – cancel any pending stop."""
        self._low_surplus_since = None
        if not self._stopped_by_us:
            self._press_attempts = 0
            self._last_press_at = float("-inf")
        elif self._monotonic() - self._last_press_at >= _PRESS_RETRY_COOLDOWN_S:
            # Our stop press never took effect and the surplus has returned: keep charging.
            _LOGGER.info(
                "EV Solar Manager: stop press did not take effect and surplus is back – keep charging"
            )
            self._stopped_by_us = False
            self._reset_press_tracking()

    async def _maybe_set_current(self, amps: int, reason: str, force: bool = False) -> None:
        """Write the new current to the charger only if the change is large enough.

        Delta suppression is bypassed when force is True – used for explicit user
        actions and state transitions (see _BYPASS_DELTA) so they always apply.
        """
        if (
            not force
            and self._last_set_current is not None
            and abs(amps - self._last_set_current) < self.min_delta_amp
        ):
            _LOGGER.debug(
                "EV Solar Manager: skipping update – delta too small: last=%sA new=%sA reason=%s",
                self._last_set_current, amps, reason,
            )
            return

        if self._read_float(self.target_number) is None:
            # HA would only log a warning and do nothing – do not record a write that never happened
            _LOGGER.warning(
                "EV Solar Manager: cannot set %sA (reason: %s) – %s is not available",
                amps, reason, self.target_number,
            )
            return

        _LOGGER.info("EV Solar Manager: setting charging current to %sA (reason: %s)", amps, reason)
        await self.hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": self.target_number, "value": float(amps)},
            blocking=True,
        )
        self._last_set_current = amps
        self._last_write_at = self._monotonic()

    async def _press_charger_button(self, reason: str) -> bool:
        """Press the charger's start/stop toggle button. Return True if the call succeeded."""
        _LOGGER.info(
            "EV Solar Manager: pressing charger button '%s' (reason: %s)",
            self.charger_start_stop_button, reason,
        )
        try:
            await self.hass.services.async_call(
                "button",
                "press",
                {"entity_id": self.charger_start_stop_button},
                blocking=True,
            )
        except Exception as ex:
            _LOGGER.error(
                "EV Solar Manager: pressing '%s' failed (reason: %s): %s",
                self.charger_start_stop_button, reason, ex,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Sensor reading helpers
    # ------------------------------------------------------------------

    def _charger_status(self) -> str:
        """Return the charger status state string, or 'unavailable' if missing."""
        if not self.charger_status_entity:
            return "unavailable"
        state = self.hass.states.get(self.charger_status_entity)
        return state.state if state else "unavailable"

    def _read_float(self, entity_id: str) -> float | None:
        """Return the numeric state of entity_id, or None if missing/unavailable/non-numeric."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in _UNAVAILABLE_STATES:
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            _LOGGER.warning(
                "EV Solar Manager: cannot parse state '%s' of %s", state.state, entity_id
            )
            return None

    # --- Time-weighted grid power average ---------------------------------
    # A single sample per tick reacts to load spikes of a few seconds (kettle,
    # compressor start). Every power_entity change is integrated over time and each
    # read returns the average since the previous read, so a short spike only moves
    # the result proportionally to its duration.

    @staticmethod
    def _parse_power(state) -> float | None:
        if state is None or state.state in _UNAVAILABLE_STATES:
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    def _accumulate_power(self, now: float) -> None:
        """Add the last known power value, weighted by the time it was valid."""
        if self._avg_last_value is not None and self._avg_last_ts is not None:
            dt = now - self._avg_last_ts
            if dt > 0:
                self._avg_integral += self._avg_last_value * dt
                self._avg_weight_s += dt
        self._avg_last_ts = now

    def _reset_power_average(self) -> None:
        """Start a new averaging window (e.g. when a timer starts after an idle period)."""
        self._avg_integral = 0.0
        self._avg_weight_s = 0.0
        self._avg_last_ts = self._monotonic()
        self._avg_last_value = self._parse_power(self.hass.states.get(self.power_entity))

    @callback
    def _handle_power_change(self, event) -> None:
        """Integrate power_entity changes into the running average."""
        self._accumulate_power(self._monotonic())
        self._avg_last_value = self._parse_power(event.data.get("new_state"))

    def _read_power_w(self) -> float | None:
        """Return the average grid power since the previous read and start a new window.

        Falls back to the instantaneous value when no time has been accumulated yet.
        Returns None if the sensor is currently unavailable or non-numeric.
        """
        self._accumulate_power(self._monotonic())
        current = self._read_float(self.power_entity)
        average = self._avg_integral / self._avg_weight_s if self._avg_weight_s > 0 else None
        self._avg_integral = 0.0
        self._avg_weight_s = 0.0
        self._avg_last_value = current
        if current is None:
            return None
        if average is not None:
            _LOGGER.debug(
                "EV Solar Manager: grid power average %.1f W (instantaneous %.1f W)", average, current
            )
            return average
        return current

    def _read_voltage(self) -> float | None:
        """Return grid voltage; None if unavailable/unknown or ≤ 0, 230 V if non-numeric."""
        state = self.hass.states.get(self.voltage_entity)
        if state is None or state.state in _UNAVAILABLE_STATES:
            return None
        try:
            voltage_v = float(state.state)
        except (ValueError, TypeError):
            _LOGGER.warning(
                "EV Solar Manager: cannot parse voltage state '%s', defaulting to %.0f V",
                state.state, _FALLBACK_VOLTAGE_V,
            )
            return _FALLBACK_VOLTAGE_V
        return voltage_v if voltage_v > 0 else None

    def _read_charger_consumption_w(self) -> float:
        """Return the charger's current power draw in Watts, or 0.0 if unavailable.

        Clamped at 0: CT meters report small negative values (e.g. -5 W) at idle.
        """
        if not self.charger_power_entity:
            return 0.0
        value = self._read_float(self.charger_power_entity)
        return max(0.0, value) if value is not None else 0.0

    def _read_available_w(self) -> tuple[float, float] | None:
        """Return (available surplus W, voltage V), or None if a sensor is unavailable.

        Used by the recovery timer to decide when to restart the charger.
        """
        power_w = self._read_power_w()
        voltage_v = self._read_voltage()
        if power_w is None or voltage_v is None:
            return None

        signed_export_w = -power_w if self.export_is_negative else power_w
        available_w = signed_export_w + self._read_charger_consumption_w() - self.safety_margin_w
        return available_w, voltage_v
