"""Folding@home v8 integration: local push via the client's websocket API."""

from __future__ import annotations

from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import FahClient
from .const import DOMAIN
from .coordinator import FahConfigEntry, FahCoordinator, FahStatsCoordinator

PLATFORMS = [Platform.BUTTON, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: FahConfigEntry) -> bool:
    """Set up a Folding@home client from a config entry."""
    client = FahClient(
        async_get_clientsession(hass), entry.data[CONF_HOST], entry.data[CONF_PORT]
    )
    coordinator = FahCoordinator(hass, entry, client)
    coordinator.stats = FahStatsCoordinator(hass, entry)
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # The machine may be off; entities show that until it comes back.
    entry.async_on_unload(coordinator.async_start())
    entry.async_on_unload(client.close)
    # Statistics come from the internet; do not delay setup for them.
    entry.async_create_background_task(
        hass, coordinator.stats.async_refresh(), f"{DOMAIN} statistics"
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FahConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
