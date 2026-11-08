"""Replay recorded client traffic (lufah sample data) through apply_update and the status logic."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
import json
from pathlib import Path

import pytest

from custom_components.foldingathome_v8.api import apply_update
from custom_components.foldingathome_v8.model import (
    STATUS_FINISHING,
    STATUS_FOLDING,
    STATUS_IDLE,
    STATUS_PAUSED,
    STATUS_WAITING,
    machine_status,
    most_urgent_unit,
    primary_unit,
    total_ppd,
    unit_deadline,
    unit_eta_seconds,
    unit_progress,
    unit_remaining_seconds,
    unit_timeout,
)

DATA = Path(__file__).parent / "data"


def _messages() -> list:
    raw = (DATA / "lufahwatch3.jsonl").read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    pos, out = 0, []
    while pos < len(raw):
        while pos < len(raw) and raw[pos].isspace():
            pos += 1
        if pos >= len(raw):
            break
        obj, pos = decoder.raw_decode(raw, pos)
        out.append(obj)
    return out


def test_replay_matches_final_snapshot() -> None:
    """Applying all updates gives the same state the recorder ended with."""
    messages = _messages()
    data = copy.deepcopy(messages[0])
    seen = set()
    for message in messages[1:]:
        if isinstance(message, list):
            apply_update(data, message)
            seen.add(machine_status(data, datetime(2024, 10, 1, 5, tzinfo=UTC)))
    final = json.loads((DATA / "lufahwatch3final.json").read_text(encoding="utf-8"))
    data.pop("log", None)
    final.pop("log", None)
    assert data == final
    assert seen  # status logic ran over every intermediate state


def _base() -> dict:
    return {
        "info": {"id": "x", "mach_name": "pc"},
        "groups": {"": {"config": {"paused": False, "finish": False}}},
        "units": [],
    }


def _unit(**kw) -> dict:
    unit = {
        "group": "",
        "state": "RUN",
        "wu_progress": 0.4567,
        "ppd": 1000,
        "eta": "2h 5m",
        "gpus": [],
    }
    unit.update(kw)
    return unit


def test_status_folding_and_ppd() -> None:
    data = _base()
    data["units"] = [_unit(), _unit(ppd=500000, gpus=["gpu:01:00:00"], eta="1d 3m 4s")]
    assert machine_status(data) == STATUS_FOLDING
    assert total_ppd(data) == 501000
    unit = primary_unit(data)
    assert unit["ppd"] == 500000
    assert unit_eta_seconds(unit) == 86400 + 180 + 4
    assert unit_progress(unit) == 45.6


def test_status_paused_and_finishing() -> None:
    data = _base()
    data["groups"][""]["config"]["paused"] = True
    data["units"] = [_unit(pause_reason="Paused")]
    assert machine_status(data) == STATUS_PAUSED
    assert total_ppd(data) == 0

    data = _base()
    data["groups"][""]["config"]["finish"] = True
    data["units"] = [_unit()]
    assert machine_status(data) == STATUS_FINISHING


def test_status_idle_and_waiting() -> None:
    now = datetime(2026, 10, 30, 12, tzinfo=UTC)
    data = _base()
    assert machine_status(data, now) == STATUS_IDLE
    data["groups"][""]["wait"] = "2026-10-30T12:05:00Z"
    assert machine_status(data, now) == STATUS_WAITING
    data = _base()
    data["units"] = [_unit(pause_reason="Waiting for idle system")]
    assert machine_status(data, now) == STATUS_WAITING


@pytest.mark.parametrize(
    ("update", "expected"),
    [
        (["units", -1, {"id": "b"}], [{"id": "a"}, {"id": "b"}]),
        (["units", 0, None], []),
        (["units", 0, "eta", "1m"], [{"id": "a", "eta": "1m"}]),
        (["units", -2, [{"id": "c"}]], [{"id": "a"}, {"id": "c"}]),
    ],
)
def test_apply_update_lists(update: list, expected: list) -> None:
    data = {"units": [{"id": "a"}]}
    apply_update(data, update)
    assert data["units"] == expected


def test_apply_update_creates_and_deletes_keys() -> None:
    data: dict = {}
    apply_update(data, ["groups", "gpu", "config", "paused", True])
    assert data == {"groups": {"gpu": {"config": {"paused": True}}}}
    apply_update(data, ["groups", "gpu", None])
    assert data == {"groups": {}}


def test_timeout_deadline_and_remaining() -> None:
    assignment = {"time": "2026-11-05T07:51:00Z", "timeout": 345600, "deadline": 432000}
    unit = _unit(eta="9h 33m", assignment=assignment)
    assert unit_timeout(unit) == datetime(2026, 11, 9, 7, 51, tzinfo=UTC)
    assert unit_deadline(unit) == datetime(2026, 11, 10, 7, 51, tzinfo=UTC)
    assert unit_remaining_seconds(unit) == 9 * 3600 + 33 * 60

    # Without an ETA: extrapolate from the computing time so far.
    unit = _unit(eta=None, wu_progress=0.25, run_time=3600, assignment=assignment)
    assert unit_remaining_seconds(unit) == 3 * 3600
    assert unit_remaining_seconds(_unit(eta=None, wu_progress=0)) is None

    # Units without an assignment (still being assigned) have no timeout.
    assert unit_timeout(_unit()) is None
    assert unit_timeout(_unit(assignment={"time": "garbage", "timeout": 10})) is None


def test_most_urgent_unit() -> None:
    data = _base()
    assert most_urgent_unit(data) is None
    late = _unit(id="late", assignment={"time": "2026-11-05T00:00:00Z", "timeout": 7200})
    soon = _unit(id="soon", assignment={"time": "2026-11-04T00:00:00Z", "timeout": 7200})
    data["units"] = [_unit(id="new"), late, soon]
    assert most_urgent_unit(data)["id"] == "soon"


def test_replayed_paused_unit_keeps_eta() -> None:
    """A paused unit in the recorded data still reports its remaining time."""
    final = json.loads((DATA / "lufahwatch3final.json").read_text(encoding="utf-8"))
    unit = final["units"][0]
    assert unit["pause_reason"] == "Paused"
    assert unit_remaining_seconds(unit) == 10 * 3600 + 9 * 60
    assert unit_timeout(unit) == datetime(2024, 10, 4, 1, 47, 53, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(37_123_456, 37_100_000), (1_230_000, 1_230_000), (57_581_875, 57_600_000), (420, 420), (0, 0)],
)
def test_ppd_rounded_to_three_digits(value: int, expected: int) -> None:
    from custom_components.foldingathome_v8.sensor import _round_significant

    assert _round_significant(value, 3) == expected
