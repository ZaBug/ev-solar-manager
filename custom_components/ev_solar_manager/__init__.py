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
  charger_start_stop_button: button.duosida_start_stop_charging  # optional: toggle button
  stopped_state: "Stopped"                       # optional: state value that means stopped/waiting (default "Stopped")
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Optional

from homeassistant.core import HomeAssistant, callback
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers.event import async_track_time_interval, async_track_state_change_event
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers import discovery
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
    DEFAULT_MIN_CURRENT,
    DEFAULT_MAX_CURRENT,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_MIN_DELTA_AMP,
    DEFAULT_EXPORT_IS_NEGATIVE,
    DEFAULT_PHASES,
    DEFAULT_SAFETY_MARGIN_W,
    DEFAULT_CHARGING_STATE,
    DEFAULT_STOPPED_STATE,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["switch", "number", "sensor", "button"]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Handle YAML configuration – trigger config flow import."""
    if DOMAIN not in config:
        return True

    # If no config entry exists yet, trigger the import flow.
    # This creates a config entry which allows proper device grouping in HA.
    if not hass.config_entries.async_entries(DOMAIN):
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": "import"},
                data=config[DOMAIN],
            )
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

    hass.data.setdefault(DOMAIN, {})
    _LOGGER.info(
        "EV Solar Manager: initializing – power_entity=%s voltage_entity=%s "
        "target_number=%s min_current=%s max_current=%s update_interval=%ss "
        "min_delta_amp=%s export_is_negative=%s phases=%s "
        "charger_power_entity=%s safety_margin_w=%s "
        "charger_status_entity=%s charging_state=%s "
        "charger_start_stop_button=%s stopped_state=%s",
        power_entity, voltage_entity, target_number,
        min_current, max_current, update_interval,
        min_delta_amp, export_is_negative, phases,
        charger_power_entity, safety_margin_w,
        charger_status_entity, charging_state,
        charger_start_stop_button, stopped_state,
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
    )
    hass.data[DOMAIN]["controller"] = controller

    await controller.async_start()
    _LOGGER.info("EV Solar Manager: controller started")

    async def _handle_stop(_event):
        await controller.async_stop()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _handle_stop)

    # Load platforms – they will pick up device_info from the config entry
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    controller = hass.data.get(DOMAIN, {}).get("controller")
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
              └─► each tick: read solar data, calculate amps, write to charger

      charger → stopped_state (e.g. "Stopped") AND _stopped_by_us is True
          └─► _start_recovery_timer()  – checks every update_interval if solar returned
              └─► surplus > 0: press start button → charger resumes → _start_timer()

      charger → any other state (Finished / Available / disconnected / etc.)
          └─► _stop_timer() + _stop_recovery_timer()  – no more API calls

    If charger_status_entity is NOT configured:
      Falls back to always-on timer (original behaviour).

    If charger_start_stop_button is NOT configured:
      Falls back to keeping min_current instead of pressing stop.

    Override mode bypasses the charging state check and stop-on-no-injection entirely.
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
        charger_power_entity: Optional[str] = None,
        safety_margin_w: float = 0.0,
        charger_status_entity: Optional[str] = None,
        charging_state: str = "Charging",
        charger_start_stop_button: Optional[str] = None,
        stopped_state: str = "Stopped",
    ) -> None:
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

        self._unsub_timer = None
        self._unsub_recovery_timer = None
        self._unsub_status_listener = None
        self._last_set_current: Optional[int] = None
        self._override_enabled: bool = False
        self._override_current: int = min_current
        self._computed_current: int = 0
        self._sensor_entity = None
        self._is_charging: bool = False   # charger is in charging_state
        self._stop_on_no_injection: bool = True   # stop charger when no solar surplus
        self._stopped_by_us: bool = False          # True when we pressed stop due to no surplus

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
        if self.charger_status_entity:
            # Watch for charging state transitions
            self._unsub_status_listener = async_track_state_change_event(
                self.hass,
                self.charger_status_entity,
                self._handle_charger_status_change,
            )
            # Delay the startup check slightly so integrations (Duosida) have time
            # to report their real state instead of 'unavailable'.
            self.hass.async_create_task(self._delayed_startup_check())
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
                "EV Solar Manager: switch.ev_solar_manager_stop_on_no_injection is enabled "
                "but charger_start_stop_button is not configured – the switch has no effect. "
                "Add charger_start_stop_button to your configuration to enable automatic stop/start."
            )

    async def _delayed_startup_check(self) -> None:
        """Wait for integrations to settle, then check charger state."""
        await asyncio.sleep(10)  # give Duosida / other integrations 10s to report real state
        current_state = self.hass.states.get(self.charger_status_entity)
        state_val = current_state.state if current_state else "unavailable"
        _LOGGER.info(
            "EV Solar Manager: startup check – charger status is '%s'", state_val
        )
        if state_val == self.charging_state:
            _LOGGER.info(
                "EV Solar Manager: charger is %s at startup – starting timer",
                self.charging_state,
            )
            self._is_charging = True
            self._start_timer()
            await self._compute_and_apply("startup")
        else:
            _LOGGER.info(
                "EV Solar Manager: charger status '%s' != '%s' – timer inactive",
                state_val, self.charging_state,
            )

    async def async_stop(self) -> None:
        """Cancel timers and all listeners on shutdown."""
        self._stop_timer()
        self._stop_recovery_timer()
        if self._unsub_status_listener:
            self._unsub_status_listener()
            self._unsub_status_listener = None

    # ------------------------------------------------------------------
    # Timer management
    # ------------------------------------------------------------------

    def _start_timer(self) -> None:
        """Start the periodic recalculation timer (idempotent)."""
        if self._unsub_timer is None:
            self._unsub_timer = async_track_time_interval(
                self.hass, self._handle_timer, timedelta(seconds=self.update_interval)
            )
            _LOGGER.debug("EV Solar Manager: recalculation timer started")

    def _stop_timer(self) -> None:
        """Stop the periodic recalculation timer (idempotent)."""
        if self._unsub_timer is not None:
            self._unsub_timer()
            self._unsub_timer = None
            _LOGGER.debug("EV Solar Manager: recalculation timer stopped")

    def _start_recovery_timer(self) -> None:
        """Start the solar-return recovery timer (idempotent)."""
        if self._unsub_recovery_timer is None:
            self._unsub_recovery_timer = async_track_time_interval(
                self.hass, self._handle_recovery_timer, timedelta(seconds=self.update_interval)
            )
            _LOGGER.debug("EV Solar Manager: solar recovery timer started")

    def _stop_recovery_timer(self) -> None:
        """Stop the solar-return recovery timer (idempotent)."""
        if self._unsub_recovery_timer is not None:
            self._unsub_recovery_timer()
            self._unsub_recovery_timer = None
            _LOGGER.debug("EV Solar Manager: solar recovery timer stopped")

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

        if new_val == self.charging_state:
            # Charger started charging → start recalculation timer
            self._is_charging = True
            self._stopped_by_us = False
            self._stop_recovery_timer()
            self._start_timer()
            self.hass.async_create_task(self._compute_and_apply("charging_started"))

        elif new_val == self.stopped_state and self._stopped_by_us:
            # We pressed stop due to no surplus → watch for solar to return
            self._is_charging = False
            self._stop_timer()
            self._last_set_current = None
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
        """Called every update_interval seconds while we wait for solar surplus to return.

        If surplus is back and charger is still in stopped_state, press start.
        """
        if not self._stopped_by_us or not self.charger_start_stop_button:
            self._stop_recovery_timer()
            return

        # Verify the charger is still in the state we expect (car still connected)
        if self.charger_status_entity:
            state = self.hass.states.get(self.charger_status_entity)
            state_val = state.state if state else "unavailable"
            if state_val != self.stopped_state:
                _LOGGER.info(
                    "EV Solar Manager: recovery timer – charger is now '%s' (not '%s') – stopping recovery",
                    state_val, self.stopped_state,
                )
                self._stopped_by_us = False
                self._stop_recovery_timer()
                return

        available_w = self._read_available_w()
        if available_w is None:
            _LOGGER.debug("EV Solar Manager: recovery timer – sensors unavailable, retrying")
            return

        if available_w > 0:
            _LOGGER.info(
                "EV Solar Manager: solar surplus returned (%.1f W) – pressing start", available_w
            )
            # Stop the recovery timer first; the status listener will start the regular timer
            # once the charger confirms it is back in charging_state.
            self._stop_recovery_timer()
            await self._press_charger_button("surplus_returned_start")
        else:
            _LOGGER.debug(
                "EV Solar Manager: recovery timer – still no surplus (%.1f W) – waiting", available_w
            )

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
            self._stopped_by_us = False
            self._stop_recovery_timer()
            if self.charger_start_stop_button:
                self.hass.async_create_task(
                    self._press_charger_button("stop_on_no_injection_disabled")
                )
        else:
            self.hass.async_create_task(self._compute_and_apply("stop_on_no_injection_toggle"))

    def set_override(self, enabled: bool) -> None:
        """Enable or disable manual override mode."""
        self._override_enabled = enabled
        self.hass.async_create_task(self._compute_and_apply("override_toggle"))

    def set_override_current(self, amps: int) -> None:
        """Set the manual override current (clamped to min/max)."""
        self._override_current = max(self.min_current, min(self.max_current, int(amps)))
        self.hass.async_create_task(self._compute_and_apply("override_value"))

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

    # ------------------------------------------------------------------
    # Core computation
    # ------------------------------------------------------------------

    async def _compute_and_apply(self, reason: str) -> None:
        """Compute the desired charging current and write it to the charger if needed."""
        try:
            # --- Override mode: bypasses all charging state and solar checks ---
            if self._override_enabled:
                target = self._override_current
                self._computed_current = target
                await self._maybe_set_current(target, reason + ":override")
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

            # --- Read source entities ---
            power_state = self.hass.states.get(self.power_entity)
            voltage_state = self.hass.states.get(self.voltage_entity)
            if not power_state or not voltage_state:
                _LOGGER.debug("EV Solar Manager: skipping calculation – source entities not yet available")
                return

            try:
                power_w = float(power_state.state)
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "EV Solar Manager: cannot parse power state '%s', defaulting to 0 W",
                    power_state.state,
                )
                power_w = 0.0

            try:
                voltage_v = float(voltage_state.state)
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "EV Solar Manager: cannot parse voltage state '%s', defaulting to 230 V",
                    voltage_state.state,
                )
                voltage_v = 230.0

            # --- Determine net grid power direction ---
            # export_is_negative=True  → sensor is negative when exporting (bidirectional meter)
            # export_is_negative=False → sensor is positive when exporting (production sensor)
            if self.export_is_negative:
                signed_export_w = -power_w  # positive = exporting, negative = importing
            else:
                signed_export_w = power_w

            # --- Compensate for EV charger load already embedded in the meter reading ---
            # grid_meter = solar - house - ev_charger  (net)
            # available  = grid_meter_export + ev_charger  (gross solar budget)
            # Priority: 1. real charger sensor  2. estimate from last set current
            charger_consumption_w = 0.0
            if self.charger_power_entity:
                charger_state = self.hass.states.get(self.charger_power_entity)
                if charger_state:
                    try:
                        charger_consumption_w = float(charger_state.state)
                    except (ValueError, TypeError):
                        charger_consumption_w = 0.0
            elif self._last_set_current is not None and voltage_v > 0:
                charger_consumption_w = self._last_set_current * voltage_v * self.phases

            available_w = signed_export_w + charger_consumption_w - self.safety_margin_w

            _LOGGER.debug(
                "EV Solar Manager: power_w=%.1f V=%.1f signed_export_w=%.1f "
                "charger_load_w=%.1f safety_margin_w=%.1f available_w=%.1f phases=%s",
                power_w, voltage_v, signed_export_w,
                charger_consumption_w, self.safety_margin_w, available_w, self.phases,
            )

            # --- Guard: no solar surplus ---
            # available_w <= 0 means we are importing from grid even without the EV.
            if available_w <= 0 and voltage_v > 0:
                if self._stop_on_no_injection and self.charger_start_stop_button:
                    # Press the toggle stop button once; recovery timer takes over from here.
                    if not self._stopped_by_us:
                        _LOGGER.info(
                            "EV Solar Manager: no solar surplus (available_w=%.1f W) – pressing stop button",
                            available_w,
                        )
                        self._stopped_by_us = True
                        await self._press_charger_button("no_surplus_stop")
                        # The status listener will see the charger enter stopped_state and
                        # start the recovery timer automatically.
                    else:
                        _LOGGER.debug(
                            "EV Solar Manager: no solar surplus (available_w=%.1f W) – already stopped by us",
                            available_w,
                        )
                else:
                    # No start/stop button configured → fall back to keeping min_current
                    amps = self.min_current
                    if self._last_set_current == self.min_current:
                        _LOGGER.debug(
                            "EV Solar Manager: no solar surplus (available_w=%.1f W) and already at min_current=%sA – skipping write",
                            available_w, self.min_current,
                        )
                        return
                    self._computed_current = amps
                    await self._maybe_set_current(amps, reason)
                    self._push_sensor_state()
                return

            elif voltage_v > 0:
                # I = P / (U × phases)
                amps = round(available_w / (voltage_v * self.phases))
                amps = max(self.min_current, min(self.max_current, amps))
            else:
                return  # no valid voltage, skip

            self._computed_current = amps
            await self._maybe_set_current(amps, reason)

            self._push_sensor_state()

        except Exception as ex:  # pragma: no cover
            _LOGGER.exception("Unexpected error in _compute_and_apply: %s", ex)

    async def _maybe_set_current(self, amps: int, reason: str) -> None:
        """Write the new current to the charger only if the change is large enough.

        Delta suppression is bypassed when the reason is one of:
          startup, charging_started, stop_on_no_injection_toggle, manual_trigger
        This ensures explicit user actions (button press, toggle) always apply.
        """
        _BYPASS_DELTA = {"startup", "charging_started", "stop_on_no_injection_toggle", "manual_trigger"}
        if (
            self._last_set_current is not None
            and abs(amps - self._last_set_current) < self.min_delta_amp
            and reason not in _BYPASS_DELTA
        ):
            _LOGGER.debug(
                "EV Solar Manager: skipping update – delta too small: last=%sA new=%sA reason=%s",
                self._last_set_current, amps, reason,
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

    async def _press_charger_button(self, reason: str) -> None:
        """Press the charger's start/stop toggle button."""
        _LOGGER.info(
            "EV Solar Manager: pressing charger button '%s' (reason: %s)",
            self.charger_start_stop_button, reason,
        )
        await self.hass.services.async_call(
            "button",
            "press",
            {"entity_id": self.charger_start_stop_button},
            blocking=True,
        )

    def _read_available_w(self) -> Optional[float]:
        """Read power/voltage sensors and return net available solar surplus watts.

        Returns None if any sensor is unavailable or unreadable.
        Used by the recovery timer to decide when to restart the charger.
        """
        power_state = self.hass.states.get(self.power_entity)
        voltage_state = self.hass.states.get(self.voltage_entity)
        if not power_state or not voltage_state:
            return None

        try:
            power_w = float(power_state.state)
        except (ValueError, TypeError):
            return None

        try:
            voltage_v = float(voltage_state.state)
        except (ValueError, TypeError):
            return None

        if voltage_v <= 0:
            return None

        signed_export_w = -power_w if self.export_is_negative else power_w

        # When charger is stopped we have no real load, but include charger_power_entity
        # reading if available (should be 0 W while stopped anyway).
        charger_consumption_w = 0.0
        if self.charger_power_entity:
            charger_state = self.hass.states.get(self.charger_power_entity)
            if charger_state:
                try:
                    charger_consumption_w = float(charger_state.state)
                except (ValueError, TypeError):
                    charger_consumption_w = 0.0

        return signed_export_w + charger_consumption_w - self.safety_margin_w
