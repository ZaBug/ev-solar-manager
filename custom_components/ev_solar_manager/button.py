"""Manual recalculation trigger button for EV Solar Manager."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
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
    """Set up the manual trigger button from a config entry."""
    controller = hass.data.get(DOMAIN, {}).get("controller")
    if not controller:
        return
    async_add_entities([EVSolarRecalcButton(controller)], True)


class EVSolarRecalcButton(ButtonEntity):
    """Button that immediately triggers a recalculation and applies the result."""

    _attr_has_entity_name = True
    _attr_name = "Recalculate Now"
    _attr_icon = "mdi:refresh"

    def __init__(self, controller) -> None:
        self._controller = controller

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_recalculate_button"

    @property
    def device_info(self):
        return ev_solar_device_info()

    async def async_press(self) -> None:
        """Trigger an immediate recalculation when the button is pressed."""
        await self._controller._compute_and_apply("manual_trigger")
