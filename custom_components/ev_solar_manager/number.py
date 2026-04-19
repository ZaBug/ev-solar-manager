"""Override current number entity for EV Solar Manager.

Allows the user to manually set the charging current that will be applied
when the override switch is turned ON.
"""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, DEFAULT_MIN_CURRENT, DEFAULT_MAX_CURRENT
from .device import ev_solar_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the override current number from a config entry."""
    controller = hass.data.get(DOMAIN, {}).get("controller")
    if not controller:
        return
    async_add_entities([EVSolarOverrideNumber(controller)], True)


class EVSolarOverrideNumber(NumberEntity):
    """Number entity to set the manual override charging current (Amperes)."""

    _attr_has_entity_name = True
    _attr_name = "Override Current"
    _attr_icon = "mdi:current-ac"
    _attr_mode = NumberMode.BOX
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "A"

    def __init__(self, controller) -> None:
        self._controller = controller

    @property
    def native_min_value(self) -> float:
        return float(self._controller.min_current or DEFAULT_MIN_CURRENT)

    @property
    def native_max_value(self) -> float:
        return float(self._controller.max_current or DEFAULT_MAX_CURRENT)

    @property
    def native_value(self) -> float | None:
        """Return the currently configured override current."""
        return float(self._controller._override_current)  # type: ignore[attr-defined]

    async def async_set_native_value(self, value: float) -> None:
        """Update the override current value in the controller."""
        self._controller.set_override_current(int(value))
        self.async_write_ha_state()

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_override_number"

    @property
    def device_info(self):
        return ev_solar_device_info()
