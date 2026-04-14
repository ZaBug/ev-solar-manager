"""Config flow for EV Solar Manager.

This integration is configured via YAML (configuration.yaml).
The config flow exists only to create a config entry, which is required
by Home Assistant to properly group entities under a single device in
Settings → Devices & Services.

No UI steps are shown to the user – the entry is created automatically
when the integration loads from YAML.
"""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant

from .const import DOMAIN


class EVSolarManagerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow that creates a single config entry for EV Solar Manager."""

    VERSION = 1

    async def async_step_import(self, import_data: dict) -> config_entries.FlowResult:
        """Handle import from configuration.yaml.

        Called automatically by HA when it finds the domain in YAML and
        a config flow exists. Creates a single entry if none exists yet.
        """
        # Prevent duplicate entries
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title="EV Solar Manager",
            data=import_data,
        )

    async def async_step_user(self, user_input=None):
        """Not used – configuration is done via YAML only."""
        return self.async_abort(reason="yaml_only")

