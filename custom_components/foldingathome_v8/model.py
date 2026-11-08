"""Derive machine status and unit details from a client snapshot."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import math
import re
from typing import Any

STATUS_FOLDING = "folding"
STATUS_FINISHING = "finishing"
STATUS_DOWNLOADING = "downloading"
STATUS_UPLOADING = "uploading"
STATUS_WAITING = "waiting"
STATUS_PAUSED = "paused"
STATUS_IDLE = "idle"
STATUS_OFFLINE = "offline"

STATUS_OPTIONS = [
    STATUS_FOLDING,
    STATUS_FINISHING,
    STATUS_DOWNLOADING,
    STATUS_UPLOADING,
    STATUS_WAITING,
    STATUS_PAUSED,
    STATUS_IDLE,
    STATUS_OFFLINE,
]

_DOWNLOAD_STATES = {"ASSIGN", "DOWNLOAD", "CORE"}
_UPLOAD_STATES = {"UPLOAD", "CLEAN"}
_ETA_PART = re.compile(r"(\d+(?:\.\d+)?)\s*([dhms])")
_ETA_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _in_future(value: Any, now: datetime) -> bool:
    when = _parse_time(value)
    return when is not None and when > now


def group_config(data: dict[str, Any], unit: dict[str, Any]) -> dict[str, Any]:
    """Return the config of the unit's resource group."""
    groups = data.get("groups") or {}
    group = groups.get(unit.get("group"))
    if group is None:
        # Units of a deleted group are moved to the default group.
        group = groups.get("", {})
    return group.get("config") or {}


def unit_is_running(data: dict[str, Any], unit: dict[str, Any]) -> bool:
    """Return True if the unit is actually computing."""
    return (
        unit.get("state") == "RUN"
        and not unit.get("pause_reason")
        and not group_config(data, unit).get("paused", False)
    )


def unit_is_gpu(unit: dict[str, Any]) -> bool:
    """Return True for a GPU work unit."""
    if unit.get("gpus"):
        return True
    assignment = unit.get("assignment") or {}
    if assignment.get("min_cpus", -1) == 0:
        return True
    core = (assignment.get("core") or {}).get("type", "")
    return isinstance(core, str) and core.startswith("0x2")


def unit_progress(unit: dict[str, Any]) -> float:
    """Return the work unit progress as a percentage."""
    value = unit.get("wu_progress", unit.get("progress", 0)) or 0
    return math.floor(float(value) * 1000) / 10


def unit_eta_seconds(unit: dict[str, Any]) -> int | None:
    """Return the client's ETA ("2h 31m") in seconds."""
    eta = unit.get("eta")
    if isinstance(eta, (int, float)):
        return int(eta)
    if not isinstance(eta, str):
        return None
    parts = _ETA_PART.findall(eta)
    if not parts:
        return None
    return int(sum(float(num) * _ETA_SECONDS[suffix] for num, suffix in parts))


def _assignment_time(unit: dict[str, Any], key: str) -> datetime | None:
    """Return assignment time + the given number of seconds (timeout/deadline)."""
    assignment = unit.get("assignment") or {}
    start = _parse_time(assignment.get("time"))
    seconds = assignment.get(key)
    if start is None or not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    return start + timedelta(seconds=seconds)


def unit_timeout(unit: dict[str, Any]) -> datetime | None:
    """Return when the work unit times out (after that it is reassigned)."""
    return _assignment_time(unit, "timeout")


def unit_deadline(unit: dict[str, Any]) -> datetime | None:
    """Return the final deadline (after that the result is worthless)."""
    return _assignment_time(unit, "deadline")


def unit_remaining_seconds(unit: dict[str, Any]) -> int | None:
    """Return the computing time still needed for the work unit.

    A paused unit keeps the ETA it had while running, which is remaining
    computing time. Without an ETA, extrapolate from the time spent so far.
    """
    eta = unit_eta_seconds(unit)
    if eta is not None:
        return eta
    progress = float(unit.get("wu_progress", unit.get("progress", 0)) or 0)
    run_time = unit.get("run_time")
    if isinstance(run_time, (int, float)) and run_time > 0 and 0 < progress < 1:
        return int(run_time * (1 - progress) / progress)
    return None


def most_urgent_unit(data: dict[str, Any]) -> dict[str, Any] | None:
    """Return the unit that times out first."""
    units = [u for u in data.get("units") or [] if unit_timeout(u) is not None]
    return min(units, key=unit_timeout) if units else None


def running_units(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the units that are computing."""
    return [u for u in data.get("units") or [] if unit_is_running(data, u)]


def total_ppd(data: dict[str, Any]) -> int:
    """Return the points per day of all running units."""
    return int(sum(u.get("ppd") or 0 for u in running_units(data)))


def primary_unit(data: dict[str, Any]) -> dict[str, Any] | None:
    """Return the running unit with the highest PPD, else the first unit."""
    running = running_units(data)
    if running:
        return max(running, key=lambda u: u.get("ppd") or 0)
    units = data.get("units") or []
    return units[0] if units else None


def machine_status(data: dict[str, Any], now: datetime | None = None) -> str:
    """Return one of STATUS_OPTIONS (except offline) for the whole machine."""
    now = now or datetime.now(UTC)
    units = data.get("units") or []
    groups = data.get("groups") or {}
    configs = [g.get("config") or {} for g in groups.values()]

    running = running_units(data)
    if running:
        if any(group_config(data, u).get("finish") for u in running):
            return STATUS_FINISHING
        return STATUS_FOLDING

    active = [
        u
        for u in units
        if not u.get("pause_reason") and not group_config(data, u).get("paused")
    ]
    if any(_in_future(u.get("wait"), now) for u in active):
        return STATUS_WAITING
    if any(u.get("state") in _DOWNLOAD_STATES for u in active):
        return STATUS_DOWNLOADING
    if any(u.get("state") in _UPLOAD_STATES for u in active):
        return STATUS_UPLOADING
    if configs and all(c.get("paused") for c in configs):
        return STATUS_PAUSED
    if any(c.get("finish") and not c.get("paused") for c in configs):
        return STATUS_FINISHING
    if any(_in_future(g.get("wait"), now) for g in groups.values()):
        return STATUS_WAITING
    if units and all(u.get("pause_reason") for u in units):
        return STATUS_WAITING
    return STATUS_IDLE


def pause_reason(data: dict[str, Any]) -> str | None:
    """Return the first pause reason reported by a unit."""
    for unit in data.get("units") or []:
        if reason := unit.get("pause_reason"):
            return str(reason)
    return None


def unit_summary(data: dict[str, Any], unit: dict[str, Any]) -> dict[str, Any]:
    """Return a compact description of a unit for state attributes."""
    assignment = unit.get("assignment") or {}
    wu = unit.get("wu") or {}
    return {
        "project": assignment.get("project"),
        "prcg": (
            f"{assignment.get('project')} "
            f"({wu.get('run')}, {wu.get('clone')}, {wu.get('gen')})"
            if assignment.get("project") is not None
            else None
        ),
        "type": "gpu" if unit_is_gpu(unit) else "cpu",
        "state": unit.get("state"),
        "running": unit_is_running(data, unit),
        "pause_reason": unit.get("pause_reason"),
        "progress": unit_progress(unit),
        "ppd": unit.get("ppd") or 0,
        "eta": unit.get("eta"),
        "cpus": unit.get("cpus"),
        "gpus": len(unit.get("gpus") or []),
        "credit": assignment.get("credit"),
    }
