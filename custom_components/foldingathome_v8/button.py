"""Buttons to fold, pause or finish on a Folding@home v8 client."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import STATE_FINISH, STATE_FOLD, STATE_PAUSE, FahConnectionError
from .const import DOMAIN
from .coordinator import FahConfigEntry, FahCoordinator
from .entity import FahEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FahConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the buttons."""
    coordinator = entry.runtime_data
    async_add_entities(
        FahStateButton(coordinator, state)
        for state in (STATE_FOLD, STATE_PAUSE, STATE_FINISH)
    )


class FahStateButton(FahEntity, ButtonEntity):
    """Send fold, pause or finish to all resource groups."""

    def __init__(self, coordinator: FahCoordinator, state: str) -> None:
        """Initialize the button."""
        super().__init__(coordinator, state)
        self._state = state
        self._attr_translation_key = state

    async def async_press(self) -> None:
        """Send the command."""
        try:
            await self.coordinator.client.send_state(self._state)
        except FahConnectionError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="not_connected"
            ) from err
