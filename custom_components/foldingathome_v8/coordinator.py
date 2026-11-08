"""Push coordinator that keeps the websocket to the client open."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import FahClient, FahConnectionError, FahError, fetch_user_stats
from .const import (
    CONF_TEAM,
    CONF_USER,
    DOMAIN,
    RECONNECT_MAX,
    RECONNECT_MIN,
    STATS_INTERVAL,
    STATS_TOLERATED_FAILURES,
    UPDATE_DEBOUNCE,
)
from .model import primary_unit, running_units, unit_eta_seconds

_LOGGER = logging.getLogger(__name__)

type FahConfigEntry = ConfigEntry[FahCoordinator]


class FahCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Holds the client snapshot and pushes changes to the entities."""

    config_entry: FahConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: FahConfigEntry, client: FahClient
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(hass, _LOGGER, config_entry=entry, name=entry.title)
        self.client = client
        self.data = {}
        self.stats: FahStatsCoordinator | None = None
        self._flush_handle: asyncio.TimerHandle | None = None
        self._eta_key: tuple[Any, Any] | None = None
        self._eta_at: datetime | None = None

    @property
    def connected(self) -> bool:
        """Return True while the client is reachable."""
        return self.client.connected

    @property
    def eta(self) -> datetime | None:
        """Return when the main running unit is expected to finish."""
        if not running_units(self.data):
            # A paused unit keeps its last ETA, which would be misleading.
            return None
        unit = primary_unit(self.data)
        if unit is None:
            return None
        key = (unit.get("id"), unit.get("eta"))
        if key != self._eta_key:
            # Only recompute when the client reports a new ETA, so the
            # timestamp does not creep forward between ETA updates.
            seconds = unit_eta_seconds(unit)
            self._eta_key = key
            self._eta_at = (
                None
                if seconds is None
                else (dt_util.utcnow() + timedelta(seconds=seconds)).replace(
                    second=0, microsecond=0
                )
            )
        return self._eta_at

    async def _async_update_data(self) -> dict[str, Any]:
        return self.client.data

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Start the connection loop; return a function that stops it."""
        task = self.config_entry.async_create_background_task(
            self.hass, self._async_run(), f"{DOMAIN} {self.client.host}"
        )

        @callback
        def _stop() -> None:
            task.cancel()
            if self._flush_handle:
                self._flush_handle.cancel()
                self._flush_handle = None

        return _stop

    async def _async_run(self) -> None:
        delay = RECONNECT_MIN
        was_connected = True
        while True:
            try:
                await self.client.connect()
            except FahConnectionError as err:
                if was_connected:
                    _LOGGER.info("%s is not reachable: %s", self.name, err)
                    was_connected = False
                    self._publish()
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX)
                continue

            if not was_connected:
                _LOGGER.info("%s is reachable again", self.name)
            was_connected = True
            delay = RECONNECT_MIN
            try:
                self._update_device()
                self._remember_donor()
                self._publish()
                await self.client.listen(self._schedule_publish)
            except FahConnectionError as err:
                _LOGGER.debug("%s: connection lost: %s", self.name, err)
            except Exception:
                # Keep the loop alive; a reconnect starts from a fresh snapshot.
                _LOGGER.exception("%s: unexpected error, reconnecting", self.name)
            finally:
                await self.client.close()
            self._publish()
            await asyncio.sleep(RECONNECT_MIN)

    @callback
    def _schedule_publish(self) -> None:
        if self._flush_handle is None:
            self._flush_handle = self.hass.loop.call_later(
                UPDATE_DEBOUNCE, self._publish
            )

    @callback
    def _publish(self) -> None:
        if self._flush_handle:
            self._flush_handle.cancel()
            self._flush_handle = None
        self.async_set_updated_data(self.client.data)

    @callback
    def _update_device(self) -> None:
        info = self.client.data.get("info") or {}
        device_registry = dr.async_get(self.hass)
        device = device_registry.async_get_device_by_identifier(
            (DOMAIN, self.config_entry.unique_id or self.config_entry.entry_id),
            self.config_entry.entry_id,
        )
        if device is None:
            return
        updates = {
            attr: value
            for attr, value in (
                ("sw_version", info.get("version")),
                ("model", info.get("cpu_brand")),
            )
            if value and getattr(device, attr) != value
        }
        if updates:
            device_registry.async_update_device(device.id, **updates)

    @callback
    def _remember_donor(self) -> None:
        """Store donor name and team so statistics work while the machine is off."""
        config = self.client.data.get("config") or {}
        user, team = config.get("user"), config.get("team")
        entry = self.config_entry
        if not user or (entry.data.get(CONF_USER), entry.data.get(CONF_TEAM)) == (
            user,
            team,
        ):
            return
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_USER: user, CONF_TEAM: team}
        )
        if self.stats:
            entry.async_create_task(self.hass, self.stats.async_request_refresh())


class FahStatsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls the public donor statistics (all machines of the donor)."""

    config_entry: FahConfigEntry

    def __init__(self, hass: HomeAssistant, entry: FahConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{entry.title} statistics",
            update_interval=STATS_INTERVAL,
        )
        self._failures = 0

    @property
    def team(self) -> dict[str, Any] | None:
        """Return the donor's statistics for the configured team."""
        teams = (self.data or {}).get("teams") or []
        team_id = self.config_entry.data.get(CONF_TEAM)
        for team in teams:
            if team.get("team") == team_id:
                return team
        return teams[0] if teams else None

    async def _async_update_data(self) -> dict[str, Any]:
        user = self.config_entry.data.get(CONF_USER)
        if not user:
            # Learned on the first connection to the client.
            return {}
        try:
            stats = await fetch_user_stats(async_get_clientsession(self.hass), user)
        except FahError as err:
            self._failures += 1
            if self.data and self._failures <= STATS_TOLERATED_FAILURES:
                _LOGGER.debug("%s; keeping the last statistics", err)
                return self.data
            raise UpdateFailed(str(err)) from err
        self._failures = 0
        return stats
