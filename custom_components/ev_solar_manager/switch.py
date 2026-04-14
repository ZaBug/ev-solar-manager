"""Override switch entity for EV Solar Manager.

When turned ON, the controller ignores solar data and charges at the current
set via the override number entity instead.
"""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_setup_platform(
    hass: HomeAssistant, config, async_add_entities, discovery_info=None
):
    """Set up the override switch platform."""
    controller = hass.data.get(DOMAIN, {}).get("controller")
    if not controller:
        return
    async_add_entities([EVSolarOverrideSwitch(controller)], True)


class EVSolarOverrideSwitch(SwitchEntity):
    """Switch that activates manual override mode."""

    _attr_name = "EV Solar Manager Override"
    _attr_icon = "mdi:hand-back-right"

    def __init__(self, controller) -> None:
        self._controller = controller

    @property
    def is_on(self) -> bool:
        """Return True when override mode is active."""
        return self._controller._override_enabled  # type: ignore[attr-defined]

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_override_switch"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, "ev_solar_manager")},
            name="EV Solar Manager",
            manufacturer="Custom",
            model="EV Solar Manager",
            sw_version="0.1.0",
        )

    async def async_turn_on(self, **kwargs) -> None:
        """Activate override mode – use the manually set current."""
        self._controller.set_override(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        """Deactivate override mode – resume solar-based control."""
        self._controller.set_override(False)
        self.async_write_ha_state()
