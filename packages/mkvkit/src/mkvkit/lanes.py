"""mkvkit.lanes -- run work concurrently, but never two heavy readers on one disk.

Two kinds of work want two different kinds of parallelism, and confusing them
is expensive in both directions.

**Requests to a server** are cheap for the machine doing the asking and the
server answers several at once without noticing. They are run through
:func:`map_bounded`: a fixed number of workers (:data:`DEFAULT_WORKERS` unless
the caller says otherwise), every item's result or error kept separately, and
the answers returned in the order the items were given. One item that fails is
one failed item, not a lost batch.

**Reading media off a disk** is the opposite. A spinning disk serves one
sequential reader well and two badly -- not half as well, badly, because the
head spends its time travelling between two positions instead of reading.
:func:`map_by_device` is the only way this repository fans out work that reads
file content: it groups the items by the device that backs them, gives every
device exactly **one** worker, and runs the devices side by side. Parallelism
across disks, never on one. There is no knob that puts a second reader on a
device, on purpose; a caller that wants one has to write it by hand and
explain it to a reviewer.

Inside a device the heaviest item goes first when a weight is given, so the
lanes finish close together instead of one disk working alone at the end.

The device of a path is its volume (:func:`mkvkit.devices.device_of`). Two
partitions of one disk are two volumes; a caller who knows better passes its
own ``device_of`` -- a table from volume to physical disk, say -- and the
grouping follows it.
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from .devices import Device, device_of

__all__ = [
    "DEFAULT_WORKERS",
    "MAX_WORKERS",
    "Outcome",
    "failures",
    "map_bounded",
    "map_by_device",
]

log = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")

#: Concurrent requests against one server when the caller names no limit.
#: Enough to hide the round trip on a local network, few enough that a
#: server busy with playback does not notice.
DEFAULT_WORKERS = 4

#: The most any caller gets, whatever it asks for. A limit exists to protect
#: the server; a limit of five hundred protects nothing.
MAX_WORKERS = 32


@dataclass(frozen=True)
class Outcome(Generic[T, R]):
    """One item's answer: its result, or the exception it raised instead."""

    item: T
    result: R | None = None
    error: BaseException | None = None
    #: the device the item was queued on, for work grouped by device
    device: Device | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def failures(outcomes: Iterable[Outcome[T, R]]) -> list[Outcome[T, R]]:
    """The outcomes that raised, in their original order."""
    return [outcome for outcome in outcomes if not outcome.ok]


def _clamp(workers: int | None) -> int:
    if workers is None:
        return DEFAULT_WORKERS
    if workers < 1:
        raise ValueError(f"at least one worker is needed, not {workers}")
    return min(workers, MAX_WORKERS)


def map_bounded(
    work: Callable[[T], R],
    items: Iterable[T],
    *,
    workers: int | None = None,
) -> list[Outcome[T, R]]:
    """Run ``work`` over every item with at most ``workers`` at a time.

    For requests, not for reading media: see :func:`map_by_device` for that.
    Results come back in the order of ``items``. An exception is caught per
    item and returned in its :class:`Outcome`, never raised from here -- except
    ``KeyboardInterrupt`` and ``SystemExit``, which stop everything.
    """
    todo = list(items)
    limit = _clamp(workers)
    if limit == 1 or len(todo) <= 1:
        return [_attempt(work, item) for item in todo]
    with ThreadPoolExecutor(max_workers=min(limit, len(todo))) as pool:
        return list(pool.map(lambda item: _attempt(work, item), todo))


def _attempt(work: Callable[[T], R], item: T, device: Device | None = None) -> Outcome[T, R]:
    try:
        return Outcome(item=item, result=work(item), device=device)
    except Exception as exc:  # the whole point is to keep it per item
        log.debug("work on %r failed: %s", item, exc)
        return Outcome(item=item, error=exc, device=device)


def map_by_device(
    work: Callable[[T], R],
    items: Iterable[T],
    *,
    path_of: Callable[[T], Path | str],
    device_of: Callable[[Path | str], Device] = device_of,
    weight_of: Callable[[T], float] | None = None,
    max_devices: int | None = None,
    before_each: Callable[[Device, T], None] | None = None,
) -> list[Outcome[T, R]]:
    """Run ``work`` with one worker per device and the devices side by side.

    The contract for anything that reads file content: **never more than one
    item in flight per device**, whatever else is going on. ``max_devices``
    caps how many devices run at once (all of them by default).
    ``before_each`` is called on the device's worker just before each item
    starts -- the place for a gate that waits until nobody else is reading
    the disk; an exception from it fails that item and the lane continues.

    Results come back in the order of ``items``, each carrying its device.
    """
    todo = list(items)
    lanes: dict[Device, list[int]] = {}
    for index, item in enumerate(todo):
        lanes.setdefault(device_of(path_of(item)), []).append(index)
    if weight_of is not None:
        for indexes in lanes.values():
            indexes.sort(key=lambda i: -weight_of(todo[i]))

    results: list[Outcome[T, R] | None] = [None] * len(todo)
    lock = threading.Lock()

    def run_lane(device: Device) -> None:
        for index in lanes[device]:
            item = todo[index]
            if before_each is not None:
                try:
                    before_each(device, item)
                except Exception as exc:  # one item, not the lane
                    outcome: Outcome[T, R] = Outcome(item=item, error=exc, device=device)
                    with lock:
                        results[index] = outcome
                    continue
            outcome = _attempt(work, item, device)
            with lock:
                results[index] = outcome

    devices: Sequence[Device] = sorted(lanes)
    log.debug("%d item(s) on %d device(s)", len(todo), len(devices))
    if len(devices) <= 1:
        for device in devices:
            run_lane(device)
    else:
        concurrent = len(devices) if max_devices is None else max(1, max_devices)
        with ThreadPoolExecutor(max_workers=min(concurrent, len(devices))) as pool:
            for future in [pool.submit(run_lane, device) for device in devices]:
                future.result()
    return [outcome for outcome in results if outcome is not None]
