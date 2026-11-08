"""Base entity for Folding@home v8."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, WEB_CONTROL_URL
from .coordinator import FahCoordinator


class FahEntity(CoordinatorEntity[FahCoordinator]):
    """Entity that belongs to one Folding@home machine."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: FahCoordinator, key: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        machine_id = entry.unique_id or entry.entry_id
        info = coordinator.client.data.get("info") or {}
        self._attr_unique_id = f"{machine_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, machine_id)},
            name=entry.title,
            manufacturer="Folding@home",
            model=info.get("cpu_brand"),
            sw_version=info.get("version"),
            configuration_url=WEB_CONTROL_URL,
        )

    @property
    def available(self) -> bool:
        """Entities are unavailable while the machine cannot be reached."""
        return self.coordinator.connected
