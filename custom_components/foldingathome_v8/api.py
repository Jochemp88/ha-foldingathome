"""Websocket client for the Folding@home v8 client (local API on port 7396)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
import json
import logging
from typing import Any
from urllib.parse import quote

import aiohttp

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 7396
CONNECT_TIMEOUT = 10
# The client sends "ping" every 15 s. Silence means the machine went to
# sleep or lost power without closing the connection, or the client is busy
# (it sends nothing while uploading a result). After this much silence, check
# whether the machine still accepts connections before giving up.
RECEIVE_TIMEOUT = 60
PROBE_TIMEOUT = 5
# A client that is awake but silent this many times in a row gets a fresh
# connection anyway.
MAX_SILENT_PROBES = 10
# The first message is a full snapshot and can be large.
MAX_MESSAGE_SIZE = 16 * 1024 * 1024

STATE_FOLD = "fold"
STATE_PAUSE = "pause"
STATE_FINISH = "finish"

STATS_URL = "https://api.foldingathome.org"
STATS_TIMEOUT = 15


class FahError(Exception):
    """Base error for the Folding@home client."""


class FahConnectionError(FahError):
    """The client could not be reached or the connection was lost."""


def apply_update(data: dict[str, Any], update: list[Any]) -> None:
    """Apply an incremental update to the snapshot.

    Port of Web Control's Updatable: all elements but the last form a key
    path, the last one is the value. A value of None deletes the item,
    index -1 (or one past the end) appends to a list, -2 extends it.
    """
    if len(update) < 2:
        return
    obj: Any = data
    for i in range(len(update) - 2):
        key = update[i]
        next_is_int = isinstance(update[i + 1], int)
        if isinstance(obj, dict) and key not in obj:
            obj[key] = [] if next_is_int else {}
        elif isinstance(obj, list) and (key == -1 or key == len(obj)):
            key = len(obj)
            obj.append([] if next_is_int else {})
        obj = obj[key]

    key, value = update[-2], update[-1]
    if isinstance(obj, list):
        if key == -2:
            obj.extend(value)
        elif key == -1 or key >= len(obj):
            obj.append(value)
        elif value is None:
            obj.pop(key)
        else:
            obj[key] = value
    elif value is None:
        obj.pop(key, None)
    else:
        obj[key] = value


async def fetch_user_stats(
    session: aiohttp.ClientSession, user: str
) -> dict[str, Any]:
    """Return the public statistics of a donor (score, wus, rank, teams)."""
    url = f"{STATS_URL}/user/{quote(user, safe='')}"
    try:
        async with asyncio.timeout(STATS_TIMEOUT):
            resp = await session.get(url)
            resp.raise_for_status()
            stats = await resp.json(content_type=None)
    except (TimeoutError, aiohttp.ClientError, ValueError) as err:
        # A timeout has no message of its own.
        raise FahError(
            f"Cannot read statistics for {user}: {str(err) or type(err).__name__}"
        ) from err
    if not isinstance(stats, dict) or "score" not in stats:
        raise FahError(f"Unexpected statistics for {user}")
    return stats


async def fetch_team_members(
    session: aiohttp.ClientSession, team: int
) -> list[dict[str, Any]]:
    """Return the top members of a team (the API lists at most 1000).

    The API answers with a table: a header row, then one row per member.
    """
    url = f"{STATS_URL}/team/{int(team)}/members"
    try:
        async with asyncio.timeout(STATS_TIMEOUT):
            resp = await session.get(url)
            resp.raise_for_status()
            rows = await resp.json(content_type=None)
    except (TimeoutError, aiohttp.ClientError, ValueError) as err:
        raise FahError(
            f"Cannot read members of team {team}: {str(err) or type(err).__name__}"
        ) from err
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], list):
        raise FahError(f"Unexpected member list for team {team}")
    header = rows[0]
    return [dict(zip(header, row)) for row in rows[1:] if isinstance(row, list)]


def member_rank(members: list[dict[str, Any]], donor_id: Any) -> int | None:
    """Return the donor's place in the team by score, or None if not listed."""
    ranked = sorted(members, key=lambda m: m.get("score") or 0, reverse=True)
    for place, member in enumerate(ranked, start=1):
        if member.get("id") == donor_id:
            return place
    return None


class FahClient:
    """Connection to one Folding@home v8 client."""

    def __init__(
        self, session: aiohttp.ClientSession, host: str, port: int = DEFAULT_PORT
    ) -> None:
        """Initialize the client."""
        self._session = session
        self.host = host
        self.port = port
        self.data: dict[str, Any] = {}
        self._ws: aiohttp.ClientWebSocketResponse | None = None

    @property
    def url(self) -> str:
        """Websocket URL of the client."""
        return f"ws://{self.host}:{self.port}/api/websocket"

    @property
    def connected(self) -> bool:
        """Return True while the websocket is open."""
        return self._ws is not None and not self._ws.closed

    async def connect(self) -> dict[str, Any]:
        """Open the websocket and read the initial snapshot."""
        await self.close()
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                self._ws = await self._session.ws_connect(
                    self.url, max_msg_size=MAX_MESSAGE_SIZE
                )
                msg = await self._ws.receive()
        except (TimeoutError, aiohttp.ClientError, OSError) as err:
            await self.close()
            raise FahConnectionError(f"Cannot connect to {self.url}: {err}") from err

        if msg.type is not aiohttp.WSMsgType.TEXT:
            await self.close()
            raise FahConnectionError(f"Unexpected first message from {self.url}")
        snapshot = json.loads(msg.data)
        if not isinstance(snapshot, dict) or "info" not in snapshot:
            await self.close()
            raise FahConnectionError(f"No client snapshot from {self.url}")
        self.data = snapshot
        return snapshot

    async def listen(self, on_change: Callable[[], None]) -> None:
        """Process messages until the connection closes."""
        ws = self._ws
        if ws is None:
            raise FahConnectionError("Not connected")
        silent = 0
        try:
            while True:
                try:
                    msg = await ws.receive(timeout=RECEIVE_TIMEOUT)
                except TimeoutError as err:
                    silent += 1
                    if silent < MAX_SILENT_PROBES and await self._accepts_connections():
                        _LOGGER.debug(
                            "%s is silent but reachable; keeping the connection",
                            self.url,
                        )
                        continue
                    raise FahConnectionError(
                        f"No message from {self.url} for {RECEIVE_TIMEOUT * silent} s"
                    ) from err
                silent = 0
                if msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
                if msg.type is not aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    message = json.loads(msg.data)
                except ValueError:
                    _LOGGER.debug("Ignoring non-JSON message: %.80s", msg.data)
                    continue
                if isinstance(message, list):
                    try:
                        apply_update(self.data, message)
                    except (KeyError, IndexError, TypeError) as err:
                        _LOGGER.debug("Ignoring update %s: %s", message, err)
                        continue
                elif isinstance(message, dict) and "info" in message:
                    self.data = message
                else:
                    # The client sends "ping" keepalives; nothing to do.
                    continue
                on_change()
        finally:
            if self._ws is ws:
                self._ws = None
            await ws.close()

    async def _accepts_connections(self) -> bool:
        """Return True if the machine accepts a new TCP connection (it is awake)."""
        try:
            async with asyncio.timeout(PROBE_TIMEOUT):
                _, writer = await asyncio.open_connection(self.host, self.port)
        except (TimeoutError, OSError):
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return True

    async def send_state(self, state: str, group: str | None = None) -> None:
        """Send fold, pause or finish to all groups, or to one group."""
        if self._ws is None or self._ws.closed:
            raise FahConnectionError("Not connected")
        time = datetime.now(UTC).replace(microsecond=0).isoformat()
        message: dict[str, Any] = {
            "cmd": "state",
            "state": state,
            "time": time.replace("+00:00", "Z"),
        }
        if group is not None:
            message["group"] = group
        try:
            await self._ws.send_str(json.dumps(message))
        except (aiohttp.ClientError, ConnectionError) as err:
            raise FahConnectionError(f"Cannot send to {self.url}: {err}") from err

    async def close(self) -> None:
        """Close the websocket."""
        ws, self._ws = self._ws, None
        if ws is not None and not ws.closed:
            await ws.close()
