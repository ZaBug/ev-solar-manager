"""Manual recalculation trigger button for EV Solar Manager."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_setup_platform(
    hass: HomeAssistant, config, async_add_entities, discovery_info=None
):
    """Set up the manual trigger button platform."""
    controller = hass.data.get(DOMAIN, {}).get("controller")
    if not controller:
        return
    async_add_entities([EVSolarRecalcButton(controller)], True)


class EVSolarRecalcButton(ButtonEntity):
    """Button that immediately triggers a recalculation and applies the result."""

    _attr_name = "EV Solar Manager Recalculate Now"
    _attr_icon = "mdi:refresh"

    def __init__(self, controller) -> None:
        self._controller = controller

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_recalculate_button"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, "ev_solar_manager")},
            name="EV Solar Manager",
            manufacturer="Custom",
            model="EV Solar Manager",
            sw_version="0.1.0",
        )

    async def async_press(self) -> None:
        """Trigger an immediate recalculation when the button is pressed."""
        await self._controller._compute_and_apply("manual_trigger")

