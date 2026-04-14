"""Computed current sensor for EV Solar Manager.

Exposes the last solar-calculated target current as a sensor entity.
The value is pushed immediately after every recalculation (no polling).
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .device import ev_solar_device_info


async def async_setup_platform(
    hass: HomeAssistant, config, async_add_entities, discovery_info=None
):
    """Set up the computed current sensor platform."""
    controller = hass.data.get(DOMAIN, {}).get("controller")
    if not controller:
        return
    sensor = EVSolarComputedCurrentSensor(controller)
    async_add_entities([sensor], True)
    # Register the sensor in the controller so it can call async_write_ha_state()
    # immediately after each calculation instead of waiting for the HA polling cycle.
    controller.register_sensor(sensor)


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
