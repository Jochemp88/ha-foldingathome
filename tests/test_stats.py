"""Tests for the donor statistics (public stats API)."""

from __future__ import annotations

import asyncio
import socket
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.foldingathome_v8.api import FahError, fetch_user_stats
from custom_components.foldingathome_v8.const import CONF_TEAM, CONF_USER, DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from .test_init import fake_client  # noqa: F401

STATS = {
    "name": "Donor",
    "id": 1,
    "score": 123456789,
    "wus": 321,
    "rank": 12345,
    "last": "2026-01-02 03:04:05",
    "users": 3000000,
    "teams": [
        {"team": 1, "name": "Other team", "trank": 5, "tscore": 10, "twus": 2},
        {"team": 4242, "name": "Example team", "trank": 64, "tscore": 99, "twus": 7},
    ],
}


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _wait_for(predicate, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


async def test_fetch_user_stats(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get("https://api.foldingathome.org/user/Donor", json=STATS)
    aioclient_mock.get(
        "https://api.foldingathome.org/user/Nobody",
        status=404,
        json={"error": "User not found.", "code": 404},
    )
    aioclient_mock.get("https://api.foldingathome.org/user/Slow", exc=TimeoutError())
    session = async_get_clientsession(hass)
    assert (await fetch_user_stats(session, "Donor"))["score"] == 123456789
    with pytest.raises(FahError):
        await fetch_user_stats(session, "Nobody")
    with pytest.raises(FahError, match="TimeoutError"):
        await fetch_user_stats(session, "Slow")


async def test_stats_sensors_while_machine_is_off(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Desktop",
        unique_id="machine-1",
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: _closed_port(),
            CONF_USER: "Donor",
            CONF_TEAM: 4242,
        },
    )
    entry.add_to_hass(hass)
    fetch = AsyncMock(return_value=STATS)
    with patch("custom_components.foldingathome_v8.coordinator.fetch_user_stats", fetch):
        assert await hass.config_entries.async_setup(entry.entry_id)
        prefix = "sensor.folding_home_statistics_"
        await _wait_for(lambda: hass.states.get(prefix + "total_points").state != "unavailable")

        assert fetch.await_args.args[1] == "Donor"
        points_state = hass.states.get(prefix + "total_points")
        assert points_state.state == "123456789"
        assert points_state.attributes["unit_of_measurement"] == "points"
        assert hass.states.get(prefix + "work_units").state == "321"
        assert hass.states.get(prefix + "rank").state == "12345"
        team = hass.states.get(prefix + "team_rank")
        assert team.state == "64"
        assert team.attributes["team"] == "Example team"
        assert (
            hass.states.get(prefix + "last_credited_work_unit").state
            == "2026-01-02T03:04:05+00:00"
        )
        # The machine itself is off; its sensors say so, statistics still work.
        assert hass.states.get("sensor.desktop_status").state == "offline"

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_stats_survive_a_short_api_outage(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Desktop",
        unique_id="machine-1",
        data={
            CONF_HOST: "127.0.0.1",
            CONF_PORT: _closed_port(),
            CONF_USER: "Donor",
            CONF_TEAM: 4242,
        },
    )
    entry.add_to_hass(hass)
    slow = FahError("Cannot read statistics for Donor: TimeoutError")
    fetch = AsyncMock(side_effect=[STATS, slow, slow, slow, STATS, slow])
    with patch("custom_components.foldingathome_v8.coordinator.fetch_user_stats", fetch):
        assert await hass.config_entries.async_setup(entry.entry_id)
        points = "sensor.folding_home_statistics_total_points"
        await _wait_for(lambda: hass.states.get(points).state != "unavailable")
        stats = entry.runtime_data.stats

        # Two failed polls in a row keep the last values, the third gives up.
        for expected in ("123456789", "123456789", "unavailable", "123456789"):
            await stats.async_refresh()
            await hass.async_block_till_done()
            assert hass.states.get(points).state == expected

        # A success resets the count.
        await stats.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(points).state == "123456789"

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_donor_learned_from_client(hass: HomeAssistant, fake_client) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Desktop",
        unique_id="machine-1",
        data={CONF_HOST: "127.0.0.1", CONF_PORT: fake_client.port},
    )
    entry.add_to_hass(hass)
    fetch = AsyncMock(return_value=STATS)
    with patch("custom_components.foldingathome_v8.coordinator.fetch_user_stats", fetch):
        assert await hass.config_entries.async_setup(entry.entry_id)
        # Unknown donor: no request yet, statistics unavailable.
        await _wait_for(lambda: entry.data.get(CONF_USER) == "someone")
        await _wait_for(lambda: fetch.await_count >= 1)
        assert fetch.await_args.args[1] == "someone"
        assert entry.data[CONF_TEAM] == 0

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
