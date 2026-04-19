"""Override and stop-on-no-injection switch entities for EV Solar Manager."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .device import ev_solar_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the override switch from a config entry."""
    controller = hass.data.get(DOMAIN, {}).get("controller")
    if not controller:
        return
    async_add_entities(
        [
            EVSolarOverrideSwitch(controller),
            EVSolarStopOnNoInjectionSwitch(controller),
        ],
        True,
    )


class EVSolarOverrideSwitch(SwitchEntity):
    """Switch that activates manual override mode."""

    _attr_has_entity_name = True
    _attr_name = "Override"
    _attr_icon = "mdi:hand-back-right"

    def __init__(self, controller) -> None:
        self._controller = controller

    @property
    def is_on(self) -> bool:
        return self._controller._override_enabled  # type: ignore[attr-defined]

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_override_switch"

    @property
    def device_info(self):
        return ev_solar_device_info()

    async def async_turn_on(self, **kwargs) -> None:
        """Activate override mode."""
        self._controller.set_override(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Deactivate override mode – resume solar-based control."""
        self._controller.set_override(False)
        self.async_write_ha_state()


class EVSolarStopOnNoInjectionSwitch(SwitchEntity):
    """Switch that controls whether charging stops when there is no solar surplus."""

    _attr_has_entity_name = True
    _attr_name = "Stop When No Solar Surplus"
    _attr_icon = "mdi:solar-power-variant"

    def __init__(self, controller) -> None:
        self._controller = controller

    @property
    def is_on(self) -> bool:
        return self._controller._stop_on_no_injection  # type: ignore[attr-defined]

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_stop_on_no_injection_switch"

    @property
    def device_info(self):
        return ev_solar_device_info()

    async def async_turn_on(self, **kwargs) -> None:
        """Enable stop-on-no-injection: charger stops when no solar surplus."""
        self._controller.set_stop_on_no_injection(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Disable stop-on-no-injection: charger stays at min_current when no surplus."""
        self._controller.set_stop_on_no_injection(False)
        self.async_write_ha_state()
