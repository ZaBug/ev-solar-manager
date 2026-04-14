"""Shared device info helper for EV Solar Manager."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN, INTEGRATION_VERSION


def ev_solar_device_info() -> DeviceInfo:
    """Return the shared DeviceInfo for all EV Solar Manager entities."""
    return DeviceInfo(
        identifiers={(DOMAIN, "ev_solar_manager")},
        name="EV Solar Manager",
        manufacturer="ZaBug",
        model="Solar EV Charger Controller",
        sw_version=INTEGRATION_VERSION,
        # mdi icon shown in the device page
        configuration_url="https://github.com/ZaBug/ev-solar-manager",
    )

