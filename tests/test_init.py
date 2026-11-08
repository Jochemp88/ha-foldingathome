"""End-to-end tests against a fake Folding@home client websocket on 127.0.0.1."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

from aiohttp import WSMsgType, web
import pytest

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant, State
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr

from custom_components.foldingathome_v8.const import DOMAIN
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

SNAPSHOT: dict[str, Any] = {
    "info": {
        "version": "8.5.5",
        "mach_name": "Desktop",
        "cpu_brand": "Test CPU",
        "id": "machine-1",
    },
    "config": {"user": "someone", "team": 0},
    "groups": {"": {"config": {"paused": False, "finish": False}}},
    "units": [
        {
            "id": "u1",
            "group": "",
            "state": "RUN",
            "wu_progress": 0.4567,
            "ppd": 1200000,
            "eta": "2h 30m",
            "gpus": ["gpu:01:00:00"],
            "assignment": {
                "project": 18201,
                "credit": 1000,
                "time": "2026-11-05T07:51:00Z",
                "timeout": 345600,
                "deadline": 432000,
            },
            "wu": {"run": 1, "clone": 2, "gen": 3},
        },
        {
            "id": "u2",
            "group": "",
            "state": "RUN",
            "wu_progress": 0.1,
            "ppd": 30000,
            "eta": "9h",
            "gpus": [],
            "assignment": {"project": 19000},
        },
    ],
}


class FakeClient:
    """Minimal stand-in for the Folding@home client's websocket API."""

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.sockets: list[web.WebSocketResponse] = []
        self.port = 0
        # While silent, new connections get no snapshot (like a sleeping PC).
        self.silent = False
        self._release = asyncio.Event()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    async def handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.append(ws)
        if self.silent:
            await self._release.wait()
            return ws
        await ws.send_str(json.dumps(SNAPSHOT))
        async for msg in ws:
            if msg.type is WSMsgType.TEXT:
                self.received.append(json.loads(msg.data))
        return ws

    async def push(self, update: Any) -> None:
        for ws in self.sockets:
            if not ws.closed:
                await ws.send_str(json.dumps(update))

    async def drop_connections(self) -> None:
        for ws in self.sockets:
            await ws.close()

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/api/websocket", self.handler)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        self.port = self._site._server.sockets[0].getsockname()[1]

    def sleep(self) -> None:
        """Stop accepting connections; open ones stay open but silent (a sleeping PC)."""
        self.silent = True
        self._site._server.close()

    async def wake(self) -> None:
        """Accept connections again on the same port."""
        self.silent = False
        self._site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        await self._site.start()

    async def stop(self) -> None:
        self._release.set()
        await self.drop_connections()
        if self._runner:
            await self._runner.cleanup()


@pytest.fixture
async def fake_client():
    client = FakeClient()
    await client.start()
    yield client
    await client.stop()


async def _wait_for(predicate, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


async def test_config_flow_creates_entry(hass: HomeAssistant, fake_client) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: " 127.0.0.1 ", CONF_PORT: fake_client.port}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Desktop"
    assert result["data"] == {CONF_HOST: "127.0.0.1", CONF_PORT: fake_client.port}
    assert result["result"].unique_id == "machine-1"

    await hass.config_entries.async_unload(result["result"].entry_id)
    await hass.async_block_till_done()


async def test_config_flow_cannot_connect(hass: HomeAssistant, fake_client) -> None:
    port = fake_client.port
    await fake_client.stop()
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: port}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_entities_buttons_and_offline(hass: HomeAssistant, fake_client) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Desktop",
        unique_id="machine-1",
        data={CONF_HOST: "127.0.0.1", CONF_PORT: fake_client.port},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.foldingathome_v8.coordinator.UPDATE_DEBOUNCE", 0.01
    ), patch("custom_components.foldingathome_v8.coordinator.RECONNECT_MIN", 0.05):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await _wait_for(lambda: hass.states.get("sensor.desktop_status").state == "folding")

        assert hass.states.get("sensor.desktop_progress").state == "45"
        assert hass.states.get("sensor.desktop_progress").attributes["project"] == 18201
        assert hass.states.get("sensor.desktop_points_per_day").state == "1230000"
        assert hass.states.get("sensor.desktop_expected_finish").state not in (
            "unknown",
            STATE_UNAVAILABLE,
        )
        units = hass.states.get("sensor.desktop_status").attributes["units"]
        assert [u["type"] for u in units] == ["gpu", "cpu"]
        assert units[0]["prcg"] == "18201 (1, 2, 3)"

        timeout = hass.states.get("sensor.desktop_work_unit_timeout")
        assert timeout.state == "2026-11-09T07:51:00+00:00"
        assert timeout.attributes["deadline"] == "2026-11-10T07:51:00+00:00"
        assert timeout.attributes["remaining_hours"] == 3  # ETA 2h 30m, rounded up
        assert timeout.attributes["prcg"] == "18201 (1, 2, 3)"

        device = dr.async_get(hass).async_get_device_by_identifier(
            (DOMAIN, "machine-1"), entry.entry_id
        )
        assert device.sw_version == "8.5.5"
        assert device.model == "Test CPU"

        await hass.services.async_call(
            "button", "press", {"entity_id": "button.desktop_pause"}, blocking=True
        )
        await _wait_for(lambda: fake_client.received)
        command = fake_client.received[0]
        assert command["cmd"] == "state"
        assert command["state"] == "pause"
        assert "group" not in command
        assert command["time"].endswith("Z")

        # The client confirms the pause with incremental updates.
        await fake_client.push(["groups", "", "config", "paused", True])
        await fake_client.push(["units", 0, "pause_reason", "Paused"])
        await fake_client.push(["units", 1, "pause_reason", "Paused"])
        await fake_client.push("ping")
        await _wait_for(lambda: hass.states.get("sensor.desktop_status").state == "paused")
        assert hass.states.get("sensor.desktop_points_per_day").state == "0"
        assert hass.states.get("sensor.desktop_expected_finish").state == "unknown"
        assert hass.states.get("sensor.desktop_progress").state == "45"

        # Machine switched off: status shows offline, the rest is unavailable,
        # except the timeout: the paused work unit is still waiting on the PC.
        await fake_client.stop()
        await _wait_for(lambda: hass.states.get("sensor.desktop_status").state == "offline")
        assert hass.states.get("sensor.desktop_progress").state == STATE_UNAVAILABLE
        assert hass.states.get("button.desktop_fold").state == STATE_UNAVAILABLE
        timeout = hass.states.get("sensor.desktop_work_unit_timeout")
        assert timeout.state == "2026-11-09T07:51:00+00:00"
        assert timeout.attributes["remaining_hours"] == 3

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_timeout_restored_while_off(hass: HomeAssistant, fake_client) -> None:
    """After a restart with the PC off, the last known timeout is restored."""
    mock_restore_cache(
        hass,
        [
            State(
                "sensor.desktop_work_unit_timeout",
                "2026-11-09T07:51:00+00:00",
                {"deadline": "2026-11-10T07:51:00+00:00", "remaining_hours": 10, "prcg": "18289 (446, 0, 1)"},
            )
        ],
    )
    port = fake_client.port
    await fake_client.stop()
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Desktop",
        unique_id="machine-1",
        data={CONF_HOST: "127.0.0.1", CONF_PORT: port},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.states.get("sensor.desktop_status").state == "offline"
    timeout = hass.states.get("sensor.desktop_work_unit_timeout")
    assert timeout.state == "2026-11-09T07:51:00+00:00"
    assert timeout.attributes["deadline"] == "2026-11-10T07:51:00+00:00"
    assert timeout.attributes["remaining_hours"] == 10
    assert timeout.attributes["prcg"] == "18289 (446, 0, 1)"

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_timeout_cleared_when_unit_is_gone(hass: HomeAssistant, fake_client) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Desktop",
        unique_id="machine-1",
        data={CONF_HOST: "127.0.0.1", CONF_PORT: fake_client.port},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.foldingathome_v8.coordinator.UPDATE_DEBOUNCE", 0.01):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await _wait_for(
            lambda: hass.states.get("sensor.desktop_work_unit_timeout").state
            == "2026-11-09T07:51:00+00:00"
        )
        # The work unit is uploaded and removed.
        await fake_client.push(["units", 0, None])
        await _wait_for(
            lambda: hass.states.get("sensor.desktop_work_unit_timeout").state == "unknown"
        )
        assert hass.states.get("sensor.desktop_work_unit_timeout").attributes.get("prcg") is None

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_silent_connection_shows_offline(hass: HomeAssistant, fake_client) -> None:
    """A PC that sleeps keeps the TCP connection; missing pings must mark it offline."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Desktop",
        unique_id="machine-1",
        data={CONF_HOST: "127.0.0.1", CONF_PORT: fake_client.port},
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.foldingathome_v8.api.RECEIVE_TIMEOUT", 0.3),
        patch("custom_components.foldingathome_v8.api.CONNECT_TIMEOUT", 0.3),
        patch("custom_components.foldingathome_v8.api.PROBE_TIMEOUT", 0.3),
        patch("custom_components.foldingathome_v8.coordinator.UPDATE_DEBOUNCE", 0.01),
        patch("custom_components.foldingathome_v8.coordinator.RECONNECT_MIN", 0.05),
        patch("custom_components.foldingathome_v8.coordinator.RECONNECT_MAX", 0.1),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await _wait_for(lambda: hass.states.get("sensor.desktop_status").state == "folding")

        # Pings keep the connection alive.
        for _ in range(4):
            await asyncio.sleep(0.15)
            await fake_client.push("ping")
        assert hass.states.get("sensor.desktop_status").state == "folding"

        # The PC goes to sleep: no more messages, connection not closed, and
        # no new connections accepted.
        fake_client.sleep()
        await _wait_for(lambda: hass.states.get("sensor.desktop_status").state == "offline")
        assert hass.states.get("sensor.desktop_progress").state == STATE_UNAVAILABLE

        # It wakes up again.
        await fake_client.wake()
        await _wait_for(lambda: hass.states.get("sensor.desktop_status").state == "folding")

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_silent_but_awake_keeps_connection(hass: HomeAssistant, fake_client) -> None:
    """The client sends nothing while uploading; that is not the PC going off."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Desktop",
        unique_id="machine-1",
        data={CONF_HOST: "127.0.0.1", CONF_PORT: fake_client.port},
    )
    entry.add_to_hass(hass)
    states: list[str] = []
    with (
        patch("custom_components.foldingathome_v8.api.RECEIVE_TIMEOUT", 0.2),
        patch("custom_components.foldingathome_v8.api.PROBE_TIMEOUT", 0.3),
        patch("custom_components.foldingathome_v8.api.MAX_SILENT_PROBES", 4),
        patch("custom_components.foldingathome_v8.coordinator.UPDATE_DEBOUNCE", 0.01),
        patch("custom_components.foldingathome_v8.coordinator.RECONNECT_MIN", 0.05),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await _wait_for(lambda: hass.states.get("sensor.desktop_status").state == "folding")
        hass.bus.async_listen(
            "state_changed",
            lambda event: states.append(event.data["new_state"].state)
            if event.data["entity_id"] == "sensor.desktop_status"
            else None,
        )

        # Two silent periods: the machine still accepts connections, so the
        # same connection is kept and the status does not flap.
        await asyncio.sleep(0.5)
        assert hass.states.get("sensor.desktop_status").state == "folding"
        assert len(fake_client.sockets) == 1
        assert "offline" not in states

        # A message resets the count.
        await fake_client.push(["units", 0, "eta", "2h 29m"])
        await asyncio.sleep(0.5)
        assert len(fake_client.sockets) == 1

        # Silent for too long: reconnect anyway.
        await _wait_for(lambda: len(fake_client.sockets) == 2)
        await _wait_for(lambda: hass.states.get("sensor.desktop_status").state == "folding")

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_reconfigure_rejects_other_machine(
    hass: HomeAssistant, fake_client
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Other",
        unique_id="machine-2",
        data={CONF_HOST: "10.0.0.9", CONF_PORT: 7396},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "127.0.0.1", CONF_PORT: fake_client.port}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_machine"


async def test_status_progress_written_once_a_minute(
    hass: HomeAssistant, fake_client
) -> None:
    """Progress ticks do not rewrite the status every few seconds; a status change does."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Desktop",
        unique_id="machine-1",
        data={CONF_HOST: "127.0.0.1", CONF_PORT: fake_client.port},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.foldingathome_v8.coordinator.UPDATE_DEBOUNCE", 0.01
    ), patch("custom_components.foldingathome_v8.coordinator.RECONNECT_MIN", 0.05):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await _wait_for(lambda: hass.states.get("sensor.desktop_status").state == "folding")
        first = hass.states.get("sensor.desktop_status")

        # Progress changes only: the status is not written again.
        for progress in (0.47, 0.48, 0.49):
            await fake_client.push(["units", 0, "wu_progress", progress])
            await fake_client.push("ping")
        await _wait_for(lambda: hass.states.get("sensor.desktop_progress").state == "49")
        assert hass.states.get("sensor.desktop_status").last_updated == first.last_updated

        # A pause is written right away.
        await fake_client.push(["groups", "", "config", "paused", True])
        await fake_client.push(["units", 0, "pause_reason", "Paused"])
        await fake_client.push(["units", 1, "pause_reason", "Paused"])
        await fake_client.push("ping")
        await _wait_for(lambda: hass.states.get("sensor.desktop_status").state == "paused")

        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
