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
from homeassistant.helpers import device_registry as dr

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
    DEFAULT_MIN_CURRENT,
    DEFAULT_MAX_CURRENT,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_MIN_DELTA_AMP,
    DEFAULT_EXPORT_IS_NEGATIVE,
    DEFAULT_PHASES,
    DEFAULT_SAFETY_MARGIN_W,
    DEFAULT_CHARGING_STATE,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["switch", "number", "sensor", "button"]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up EV Solar Manager from YAML configuration."""

    if DOMAIN not in config:
        return True

    cfg = config.get(DOMAIN) or {}

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

    hass.data.setdefault(DOMAIN, {})
    _LOGGER.info(
        "EV Solar Manager: initializing – power_entity=%s voltage_entity=%s "
        "target_number=%s min_current=%s max_current=%s update_interval=%ss "
        "min_delta_amp=%s export_is_negative=%s phases=%s "
        "charger_power_entity=%s safety_margin_w=%s "
        "charger_status_entity=%s charging_state=%s",
        power_entity, voltage_entity, target_number,
        min_current, max_current, update_interval,
        min_delta_amp, export_is_negative, phases,
        charger_power_entity, safety_margin_w,
        charger_status_entity, charging_state,
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
    )
    hass.data[DOMAIN]["controller"] = controller
    hass.data[DOMAIN]["config"] = {
        CONF_POWER_ENTITY: power_entity,
        CONF_VOLTAGE_ENTITY: voltage_entity,
        CONF_TARGET_NUMBER: target_number,
        CONF_MIN_CURRENT: min_current,
        CONF_MAX_CURRENT: max_current,
        CONF_UPDATE_INTERVAL: update_interval,
        CONF_MIN_DELTA_AMP: min_delta_amp,
        CONF_EXPORT_IS_NEGATIVE: export_is_negative,
        CONF_PHASES: phases,
        CONF_CHARGER_POWER_ENTITY: charger_power_entity,
        CONF_SAFETY_MARGIN_W: safety_margin_w,
        CONF_CHARGER_STATUS_ENTITY: charger_status_entity,
        CONF_CHARGING_STATE: charging_state,
    }

    await controller.async_start()
    _LOGGER.info("EV Solar Manager: controller started")

    # Register the device explicitly in the device registry so all entities
    # are grouped under a single device entry in Settings → Devices & Services.
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=None,
        identifiers={(DOMAIN, "ev_solar_manager")},
        name="EV Solar Manager",
        manufacturer="ZaBug",
        model="Solar EV Charger Controller",
        sw_version=hass.data[DOMAIN]["config"].get("version", "1.0.1"),
        configuration_url="https://github.com/ZaBug/ev-solar-manager",
    )

    async def _handle_stop(_event):
        await controller.async_stop()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _handle_stop)

    for platform in PLATFORMS:
        _LOGGER.debug("EV Solar Manager: loading platform %s", platform)
        hass.async_create_task(
            discovery.async_load_platform(hass, platform, DOMAIN, {}, config)
        )

    return True


class EVSolarController:
    """Compute the target EV charging current from solar export power and voltage.

    State machine
    -------------
    If charger_status_entity is configured:

      sensor.duosida_status == charging_state (e.g. "Charging")
          └─► _start_timer()  – periodic recalculation every update_interval seconds
              └─► each tick: read solar data, calculate amps, write to charger
                  (only if solar production > 0, otherwise skip to avoid noise)

      sensor.duosida_status != charging_state (Finished / Available / unavailable)
          └─► _stop_timer()   – no more API calls, no log noise

    If charger_status_entity is NOT configured:
      Falls back to always-on timer (original behaviour).

    Override mode bypasses the charging state check entirely.
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

        self._unsub_timer = None
        self._unsub_status_listener = None
        self._last_set_current: Optional[int] = None
        self._override_enabled: bool = False
        self._override_current: int = min_current
        self._computed_current: int = 0
        self._sensor_entity = None
        self._is_charging: bool = False  # tracks whether charger is actively charging

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
                "EV Solar Manager: charger status '%s' ≠ '%s' – timer inactive",
                state_val, self.charging_state,
            )

    async def async_stop(self) -> None:
        """Cancel timer and all listeners on shutdown."""
        self._stop_timer()
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
            # Charger started charging → start timer and compute immediately
            self._is_charging = True
            self._start_timer()
            self.hass.async_create_task(self._compute_and_apply("charging_started"))
        else:
            # Charger stopped / finished / disconnected → stop timer
            self._is_charging = False
            self._stop_timer()
            # Reset last set current so next charge session starts fresh
            self._last_set_current = None
            _LOGGER.info(
                "EV Solar Manager: timer stopped – no active charging session"
            )

    # ------------------------------------------------------------------
    # Timer callback
    # ------------------------------------------------------------------

    async def _handle_timer(self, now) -> None:
        """Called every update_interval seconds while charger is active."""
        await self._compute_and_apply("timer")

    # ------------------------------------------------------------------
    # Public API (used by switch / number / button entities)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Core computation
    # ------------------------------------------------------------------

    async def _compute_and_apply(self, reason: str) -> None:
        """Compute the desired charging current and write it to the charger if needed."""
        try:
            # --- Override mode: bypasses charging state check ---
            if self._override_enabled:
                target = self._override_current
                self._computed_current = target
                await self._maybe_set_current(target, reason + ":override")
                if self._sensor_entity is not None:
                    self._sensor_entity.async_write_ha_state()
                return

            # --- Guard: only act when charger is actively charging ---
            # (when charger_status_entity is set; otherwise _is_charging is always True)
            if not self._is_charging:
                _LOGGER.debug(
                    "Skipping calculation: charger is not in '%s' state", self.charging_state
                )
                return

            # --- Read source entities ---
            power_state = self.hass.states.get(self.power_entity)
            voltage_state = self.hass.states.get(self.voltage_entity)
            if not power_state or not voltage_state:
                _LOGGER.debug("Skipping calculation: source entities not yet available")
                return

            try:
                power_w = float(power_state.state)
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "Cannot parse power state '%s', defaulting to 0 W", power_state.state
                )
                power_w = 0.0

            try:
                voltage_v = float(voltage_state.state)
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "Cannot parse voltage state '%s', defaulting to 230 V", voltage_state.state
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
                "power_w=%.1f V=%.1f signed_export_w=%.1f "
                "charger_load_w=%.1f safety_margin_w=%.1f available_w=%.1f phases=%s",
                power_w, voltage_v, signed_export_w,
                charger_consumption_w, self.safety_margin_w, available_w, self.phases,
            )

            # --- Guard: skip if no solar production at all ---
            # available_w ≤ 0 means we are importing from the grid even without the EV.
            # Keep the charger at min_current but do NOT write if already there –
            # avoids pointless API calls at night or on cloudy days.
            if available_w <= 0 and voltage_v > 0:
                amps = self.min_current
                if self._last_set_current == self.min_current:
                    _LOGGER.debug(
                        "No solar production (available_w=%.1f) and already at min_current=%sA – skipping write",
                        available_w, self.min_current,
                    )
                    return
            elif voltage_v > 0:
                # I = P / (U × phases)
                amps = round(available_w / (voltage_v * self.phases))
                amps = max(self.min_current, min(self.max_current, amps))
            else:
                return  # no valid voltage, skip

            self._computed_current = amps
            await self._maybe_set_current(amps, reason)

            if self._sensor_entity is not None:
                self._sensor_entity.async_write_ha_state()

        except Exception as ex:  # pragma: no cover
            _LOGGER.exception("Unexpected error in _compute_and_apply: %s", ex)

    async def _maybe_set_current(self, amps: int, reason: str) -> None:
        """Write the new current to the charger only if the change is large enough."""
        if (
            self._last_set_current is not None
            and abs(amps - self._last_set_current) < self.min_delta_amp
            and reason != "startup"
            and reason != "charging_started"
        ):
            _LOGGER.debug(
                "Skipping update – delta too small: last=%sA new=%sA reason=%s",
                self._last_set_current, amps, reason,
            )
            return

        _LOGGER.info("Setting charging current to %s A (reason: %s)", amps, reason)
        await self.hass.services.async_call(
            "number",
            "set_value",
            {"entity_id": self.target_number, "value": float(amps)},
            blocking=True,
        )
        self._last_set_current = amps
