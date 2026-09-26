"""Bounded fan-out for requests, and one reader per device for everything else."""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import threading
import time
from collections import defaultdict
from pathlib import Path

import pytest
from mkvkit.devices import device_of, same_device
from mkvkit.lanes import (
    DEFAULT_WORKERS,
    MAX_WORKERS,
    failures,
    map_bounded,
    map_by_device,
)


class Meter:
    """Counts how many calls are in flight, overall and per key."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.now: dict[str, int] = defaultdict(int)
        self.peak: dict[str, int] = defaultdict(int)

    def enter(self, key: str) -> None:
        with self.lock:
            self.now[key] += 1
            self.now["*"] += 1
            for name in (key, "*"):
                self.peak[name] = max(self.peak[name], self.now[name])

    def leave(self, key: str) -> None:
        with self.lock:
            self.now[key] -= 1
            self.now["*"] -= 1


def _disk(path: Path | str) -> str:
    """A stand-in device table: the first path segment names the disk."""
    return str(path).split("/", 1)[0]


def test_requests_run_side_by_side_but_never_past_the_limit() -> None:
    meter = Meter()

    def work(item: int) -> int:
        meter.enter("server")
        time.sleep(0.01)
        meter.leave("server")
        return item * 2

    outcomes = map_bounded(work, range(12), workers=3)
    assert [o.result for o in outcomes] == [i * 2 for i in range(12)]
    assert 1 < meter.peak["*"] <= 3


def test_one_failing_request_is_one_failed_item() -> None:
    def work(item: int) -> int:
        if item == 2:
            raise ConnectionError("refused")
        return item

    outcomes = map_bounded(work, range(5), workers=2)
    assert [o.ok for o in outcomes] == [True, True, False, True, True]
    assert [o.item for o in failures(outcomes)] == [2]
    assert isinstance(outcomes[2].error, ConnectionError)


def test_the_worker_limit_has_a_default_a_ceiling_and_a_floor() -> None:
    assert 1 <= DEFAULT_WORKERS <= MAX_WORKERS
    with pytest.raises(ValueError):
        map_bounded(lambda item: item, [1], workers=0)
    assert [o.result for o in map_bounded(lambda i: i, [1, 2], workers=10_000)] == [1, 2]


def test_never_two_readers_on_one_disk_but_disks_side_by_side() -> None:
    meter = Meter()
    items = [f"{disk}/file-{n}" for n in range(4) for disk in ("one", "two", "three")]

    def work(path: str) -> str:
        meter.enter(_disk(path))
        time.sleep(0.01)
        meter.leave(_disk(path))
        return path

    outcomes = map_by_device(work, items, path_of=lambda p: p, device_of=_disk)
    assert [o.item for o in outcomes] == items
    assert [o.device for o in outcomes] == [_disk(p) for p in items]
    assert meter.peak["one"] == meter.peak["two"] == meter.peak["three"] == 1
    assert meter.peak["*"] > 1


def test_devices_can_be_capped_too() -> None:
    meter = Meter()
    items = [f"{disk}/x" for disk in "abcd"]

    def work(path: str) -> None:
        meter.enter(_disk(path))
        time.sleep(0.01)
        meter.leave(_disk(path))

    map_by_device(work, items, path_of=lambda p: p, device_of=_disk, max_devices=1)
    assert meter.peak["*"] == 1


def test_the_heaviest_item_on_a_device_goes_first() -> None:
    order: list[str] = []
    weights = {"one/small": 1.0, "one/large": 9.0, "one/middle": 5.0}
    map_by_device(order.append, list(weights), path_of=lambda p: p,
                  device_of=_disk, weight_of=weights.__getitem__)
    assert order == ["one/large", "one/middle", "one/small"]


def test_a_gate_that_refuses_fails_its_item_and_the_lane_goes_on() -> None:
    def gate(device: str, item: str) -> None:
        if item.endswith("held"):
            raise RuntimeError(f"{device} is busy")

    outcomes = map_by_device(lambda p: p, ["one/held", "one/free"],
                             path_of=lambda p: p, device_of=_disk, before_each=gate)
    assert [o.ok for o in outcomes] == [False, True]
    assert "busy" in str(outcomes[0].error)


def test_a_path_resolves_to_its_volume(tmp_path: Path) -> None:
    assert same_device(tmp_path, tmp_path / "not-yet-there")
    assert device_of(tmp_path) == device_of(tmp_path / "a" / "b")
