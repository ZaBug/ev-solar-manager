"""Computed current sensor for EV Solar Manager.

Exposes the last solar-calculated target current as a sensor entity.
The value is pushed immediately after every recalculation (no polling).
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
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
    """Set up the computed current sensor from a config entry."""
    controller = hass.data.get(DOMAIN, {}).get("controller")
    if not controller:
        return
    sensor = EVSolarComputedCurrentSensor(controller)
    async_add_entities([sensor], True)
    # NOTE: controller.register_sensor() is called inside async_added_to_hass()
    # to ensure the entity has a valid entity_id before any state writes.


class EVSolarComputedCurrentSensor(SensorEntity):
    """Sensor that shows the current calculated from solar surplus."""

    _attr_has_entity_name = True
    _attr_name = "Computed Current"
    _attr_icon = "mdi:current-ac"
    _attr_native_unit_of_measurement = "A"
    _attr_state_class = "measurement"
    _attr_should_poll = False  # state is pushed by the controller after each calculation

    def __init__(self, controller) -> None:
        self._controller = controller

    async def async_added_to_hass(self) -> None:
        """Register with the controller once the entity has a valid entity_id."""
        await super().async_added_to_hass()
        self._controller.register_sensor(self)

    async def async_will_remove_from_hass(self) -> None:
        """Unregister from the controller when removed."""
        self._controller.register_sensor(None)

    @property
    def native_value(self):
        """Return the last computed charging current in Amperes."""
        return self._controller.get_computed_current()

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_computed_current"

    @property
    def device_info(self):
        return ev_solar_device_info()
