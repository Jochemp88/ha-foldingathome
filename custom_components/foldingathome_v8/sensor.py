"""Sensors for Folding@home v8."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
import math
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONF_USER, DOMAIN, STATUS_DETAILS_INTERVAL
from .coordinator import FahConfigEntry, FahCoordinator, FahStatsCoordinator
from .entity import FahEntity
from .model import (
    STATUS_OFFLINE,
    STATUS_OPTIONS,
    machine_status,
    most_urgent_unit,
    pause_reason,
    primary_unit,
    running_units,
    total_ppd,
    unit_deadline,
    unit_progress,
    unit_remaining_seconds,
    unit_summary,
    unit_timeout,
)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FahConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            FahStatusSensor(coordinator),
            FahProgressSensor(coordinator),
            FahPpdSensor(coordinator),
            FahEtaSensor(coordinator),
            FahTimeoutSensor(coordinator),
        ]
    )
    if coordinator.stats:
        async_add_entities(
            FahStatsSensor(coordinator.stats, description)
            for description in STATS_SENSORS
        )


class FahStatusSensor(FahEntity, SensorEntity):
    """Overall status of the machine; stays available while it is off."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUS_OPTIONS
    _attr_translation_key = "status"
    # Unit details change every minute; keep them out of the database.
    _unrecorded_attributes = frozenset({"units", "pause_reason"})

    def __init__(self, coordinator: FahCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, "status")
        self._written_key: tuple[Any, ...] | None = None
        self._written_at: datetime | None = None

    @property
    def available(self) -> bool:
        """Always available, so the dashboard can show that the machine is off."""
        return True

    @callback
    def _handle_coordinator_update(self) -> None:
        """Write right away when the status, pause reason or set of units changes.

        Unit progress in the attributes changes every few seconds while folding.
        Every write is a new row in the recorder (unrecorded attributes or not)
        and wakes state triggers, so those details are written once a minute.
        """
        data = self.coordinator.data
        key = (
            self.native_value,
            pause_reason(data) if self.coordinator.connected else None,
            tuple(u.get("id") for u in data.get("units") or []),
        )
        now = dt_util.utcnow()
        if (
            key == self._written_key
            and self._written_at is not None
            and now - self._written_at < STATUS_DETAILS_INTERVAL
        ):
            return
        self._written_key, self._written_at = key, now
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> str:
        """Return the machine status."""
        if not self.coordinator.connected:
            return STATUS_OFFLINE
        return machine_status(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the work units and pause reason."""
        if not self.coordinator.connected:
            return None
        data = self.coordinator.data
        return {
            "units": [unit_summary(data, u) for u in data.get("units") or []],
            "pause_reason": pause_reason(data),
        }


class FahProgressSensor(FahEntity, SensorEntity):
    """Progress of the main work unit."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision = 0
    _attr_translation_key = "progress"

    def __init__(self, coordinator: FahCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, "progress")

    @property
    def native_value(self) -> int | None:
        """Return whole percents, so the state changes about 100 times per unit."""
        unit = primary_unit(self.coordinator.data)
        if unit is None:
            return None
        return int(unit_progress(unit))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the project of the main work unit."""
        unit = primary_unit(self.coordinator.data)
        if unit is None:
            return None
        summary = unit_summary(self.coordinator.data, unit)
        return {
            "project": summary["project"],
            "type": summary["type"],
            "gpu": summary["gpu"],
        }


class FahPpdSensor(FahEntity, SensorEntity):
    """Points per day of all running work units."""

    _attr_native_unit_of_measurement = "PPD"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "ppd"

    def __init__(self, coordinator: FahCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, "ppd")

    @property
    def native_value(self) -> int:
        """Return the total points per day, rounded to 3 significant digits.

        The client re-estimates PPD every few seconds; rounding keeps the
        recorder from storing hundreds of near-identical states per hour.
        """
        return _round_significant(total_ppd(self.coordinator.data), 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the number of running units."""
        return {"running_units": len(running_units(self.coordinator.data))}


class FahEtaSensor(FahEntity, SensorEntity):
    """Expected finish time of the main work unit."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "eta"

    def __init__(self, coordinator: FahCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, "eta")

    @property
    def native_value(self) -> datetime | None:
        """Return the expected finish time."""
        return self.coordinator.eta


class FahTimeoutSensor(FahEntity, SensorEntity, RestoreEntity):
    """When the work unit that is due first times out.

    Keeps the last known value while the machine is off (and across restarts),
    so automations can decide to wake the machine before it is too late.
    """

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "timeout"

    def __init__(self, coordinator: FahCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, "timeout")
        self._value: datetime | None = None
        self._attrs: dict[str, Any] = {}

    async def async_added_to_hass(self) -> None:
        """Read the current unit, or restore the last one while the machine is off."""
        await super().async_added_to_hass()
        if self.coordinator.connected:
            self._refresh()
            return
        last = await self.async_get_last_state()
        if last is None or (when := dt_util.parse_datetime(last.state)) is None:
            return
        self._value = when
        self._attrs = {key: last.attributes.get(key) for key in TIMEOUT_ATTRIBUTES}

    @property
    def available(self) -> bool:
        """Always available; the work unit is still there while the machine is off."""
        return True

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.coordinator.connected:
            self._refresh()
        super()._handle_coordinator_update()

    def _refresh(self) -> None:
        data = self.coordinator.data
        unit = most_urgent_unit(data)
        if unit is None:
            self._value, self._attrs = None, {}
            return
        deadline = unit_deadline(unit)
        remaining = unit_remaining_seconds(unit)
        self._value = unit_timeout(unit)
        self._attrs = {
            "deadline": deadline.isoformat() if deadline else None,
            # Whole hours: changes about once an hour while folding.
            "remaining_hours": (
                math.ceil(remaining / 3600) if remaining is not None else None
            ),
            "prcg": unit_summary(data, unit)["prcg"],
        }

    @property
    def native_value(self) -> datetime | None:
        """Return the timeout of the work unit that is due first."""
        return self._value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the deadline, computing time left and work unit."""
        return self._attrs or None


TIMEOUT_ATTRIBUTES = ("deadline", "remaining_hours", "prcg")


def _round_significant(value: int, digits: int) -> int:
    """Round a positive number to the given number of significant digits."""
    if value <= 0:
        return 0
    return int(round(value, digits - 1 - int(math.floor(math.log10(value)))))


def _parse_last(value: Any) -> datetime | None:
    """Parse the stats API time ("2026-01-02 03:04:05", UTC)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=UTC)
    except ValueError:
        return None


@dataclass(frozen=True, kw_only=True)
class FahStatsSensorDescription(SensorEntityDescription):
    """Describes a donor statistics sensor."""

    value_fn: Callable[[FahStatsCoordinator], Any]
    attrs_fn: Callable[[FahStatsCoordinator], dict[str, Any] | None] = (
        lambda _: None
    )


def _team_attrs(stats: FahStatsCoordinator) -> dict[str, Any] | None:
    team = stats.team
    if team is None:
        return None
    return {
        "team": team.get("name"),
        "team_id": team.get("team"),
        "team_score": team.get("tscore"),
        "team_wus": team.get("twus"),
    }


STATS_SENSORS: tuple[FahStatsSensorDescription, ...] = (
    FahStatsSensorDescription(
        key="score",
        translation_key="score",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda s: s.data.get("score"),
    ),
    FahStatsSensorDescription(
        key="wus",
        translation_key="wus",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda s: s.data.get("wus"),
    ),
    FahStatsSensorDescription(
        key="rank",
        translation_key="rank",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.data.get("rank"),
        attrs_fn=lambda s: {"donors": s.data.get("users")},
    ),
    FahStatsSensorDescription(
        key="team_rank",
        translation_key="team_rank",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: (s.team or {}).get("trank"),
        attrs_fn=_team_attrs,
    ),
    FahStatsSensorDescription(
        key="team_member_rank",
        translation_key="team_member_rank",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.data.get("team_member_rank"),
        attrs_fn=lambda s: {"team": (s.team or {}).get("name")},
    ),
    FahStatsSensorDescription(
        key="last_wu",
        translation_key="last_wu",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda s: _parse_last(s.data.get("last")),
    ),
)


class FahStatsSensor(CoordinatorEntity[FahStatsCoordinator], SensorEntity):
    """Public statistics of the donor that this client folds for."""

    _attr_has_entity_name = True
    entity_description: FahStatsSensorDescription

    def __init__(
        self, coordinator: FahStatsCoordinator, description: FahStatsSensorDescription
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        entry = coordinator.config_entry
        base = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{base}_stats_{description.key}"
        user = entry.data.get(CONF_USER)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{base}_stats")},
            translation_key="stats",
            manufacturer="Folding@home",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=(
                f"https://stats.foldingathome.org/donor/name/{user}"
                if user
                else "https://stats.foldingathome.org/"
            ),
        )

    @property
    def available(self) -> bool:
        """Available once statistics have been read."""
        return super().available and bool(self.coordinator.data)

    @property
    def native_value(self) -> Any:
        """Return the statistic."""
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra details, such as the team name."""
        return self.entity_description.attrs_fn(self.coordinator)
