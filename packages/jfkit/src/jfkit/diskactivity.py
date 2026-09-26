"""jfkit.diskactivity -- is anybody using this disk right now, by its own counters.

The process list answers "who could be reading this device", and on one
platform it cannot answer it for the programs that matter most: a server that
runs as another account starts its decoders with command lines an ordinary
user is not allowed to read. The name says "a decoder is running"; nothing
says which disk it is on.

The disk can say. Every platform keeps running totals per volume -- bytes
read and written, and how long the device was busy -- and two readings a
second apart are a measurement of what everybody together did with it in that
second. Taken while the caller's own reader for that device is between items,
anything the counters show is somebody else.

Both readings are unprivileged:

* on Windows, the volume's own performance counters (the disk-performance
  device control on ``\\\\.\\X:``, opened with no access rights at all);
* on Linux, the line for the block device behind the mount point in the
  kernel's disk statistics.

Anywhere else, or when either is refused, the answer is ``None``: unknown,
which a gate must not read as "quiet".
"""

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

from __future__ import annotations

import logging
import os
import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePath

from .devices import Device

__all__ = [
    "Activity",
    "BusyLimits",
    "Counters",
    "between",
    "counters",
    "parse_diskstats",
    "sample",
]

log = logging.getLogger(__name__)

MIB = 1 << 20


@dataclass(frozen=True)
class Counters:
    """Running totals for one device, as the platform keeps them."""

    read_bytes: int
    written_bytes: int
    #: seconds, on the clock ``at_s`` is on; None where the platform does not say
    busy_s: float | None
    #: when the reading was taken, in seconds on a clock that only moves forward
    at_s: float
    #: requests in flight at that instant, where the platform says
    queue: int | None = None


@dataclass(frozen=True)
class Activity:
    """What a device did between two readings."""

    device: Device
    window_s: float
    read_bps: float
    write_bps: float
    #: share of the window the device was busy, 0 to 1; None where unknown
    busy: float | None = None
    queue: int | None = None

    def __str__(self) -> str:
        parts = [
            f"read {self.read_bps / MIB:.1f} MiB/s",
            f"wrote {self.write_bps / MIB:.1f} MiB/s",
        ]
        if self.busy is not None:
            parts.append(f"busy {self.busy:.0%}")
        if self.queue:
            parts.append(f"{self.queue} request(s) queued")
        return ", ".join(parts) + f" over {self.window_s:.1f} s"


@dataclass(frozen=True)
class BusyLimits:
    """Where a device stops being quiet.

    A film played straight off a disk reads a few megabytes a second in
    bursts; a preview or chapter-image pass reads as fast as the disk goes.
    The defaults call the first quiet-ish and the second busy -- playback has
    a signal of its own that is exact, and this one is for the work nobody
    announces.
    """

    #: read plus written, per second
    max_bytes_per_s: float = 4 * MIB
    #: share of the window the device may be busy
    max_busy: float = 0.30

    def reason(self, activity: Activity) -> str | None:
        """Why this activity is too much, or None when it is quiet."""
        moved = activity.read_bps + activity.write_bps
        if moved > self.max_bytes_per_s:
            return (
                f"{activity}: more than {self.max_bytes_per_s / MIB:.1f} MiB/s is "
                "moving on this disk"
            )
        if activity.busy is not None and activity.busy > self.max_busy:
            return f"{activity}: the disk was busy for more than {self.max_busy:.0%} of it"
        return None

    def quiet(self, activity: Activity) -> bool:
        return self.reason(activity) is None


# ------------------------------------------------------------------ readings
def counters(device: Device) -> Counters | None:
    """The device's running totals now, or None where they cannot be read."""
    try:
        if os.name == "nt":
            return _windows_counters(device)
        if sys.platform == "linux":
            return _linux_counters(device)
    except OSError as exc:
        log.debug("disk counters for %s unreadable: %s", device, exc)
    return None


def sample(
    device: Device,
    *,
    window_s: float = 1.0,
    read: Callable[[Device], Counters | None] = counters,
    sleep: Callable[[float], None] = time.sleep,
) -> Activity | None:
    """Two readings ``window_s`` apart, as rates; None when either is unavailable."""
    first = read(device)
    if first is None:
        return None
    sleep(window_s)
    second = read(device)
    if second is None:
        return None
    return between(device, first, second)


def between(device: Device, first: Counters, second: Counters) -> Activity:
    """The activity two readings of one device imply."""
    span = second.at_s - first.at_s
    if span <= 0:
        span = 1e-9
    busy: float | None = None
    if first.busy_s is not None and second.busy_s is not None:
        busy = min(1.0, max(0.0, (second.busy_s - first.busy_s) / span))
    return Activity(
        device=device,
        window_s=span,
        read_bps=max(0, second.read_bytes - first.read_bytes) / span,
        write_bps=max(0, second.written_bytes - first.written_bytes) / span,
        busy=busy,
        queue=second.queue,
    )


# ------------------------------------------------------------------- Windows
#: the device control that returns a volume's performance totals
_DISK_PERFORMANCE = 0x70020
#: the counters count in units of a hundred nanoseconds
_TICKS_PER_S = 10_000_000


def _windows_counters(device: Device) -> Counters | None:  # pragma: no cover - platform
    import ctypes
    from ctypes import wintypes

    if not re.fullmatch(r"[A-Za-z]:", device):
        return None

    class DiskPerformance(ctypes.Structure):
        _fields_ = [  # the layout the device control fills
            ("BytesRead", ctypes.c_longlong),
            ("BytesWritten", ctypes.c_longlong),
            ("ReadTime", ctypes.c_longlong),
            ("WriteTime", ctypes.c_longlong),
            ("IdleTime", ctypes.c_longlong),
            ("ReadCount", wintypes.DWORD),
            ("WriteCount", wintypes.DWORD),
            ("QueueDepth", wintypes.DWORD),
            ("SplitCount", wintypes.DWORD),
            ("QueryTime", ctypes.c_longlong),
            ("StorageDeviceNumber", wintypes.DWORD),
            ("StorageManagerName", wintypes.WCHAR * 8),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
    ]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    share_read_write = 0x1 | 0x2
    open_existing = 3
    handle = kernel.CreateFileW(
        "\\\\.\\" + device.upper(), 0, share_read_write, None, open_existing, 0, None
    )
    if handle is None or handle == wintypes.HANDLE(-1).value:
        return None
    try:
        found = DiskPerformance()
        returned = wintypes.DWORD()
        if not kernel.DeviceIoControl(
            handle, _DISK_PERFORMANCE, None, 0, ctypes.byref(found),
            ctypes.sizeof(found), ctypes.byref(returned), None,
        ):
            return None
    finally:
        kernel.CloseHandle(handle)
    at = found.QueryTime / _TICKS_PER_S
    return Counters(
        read_bytes=int(found.BytesRead),
        written_bytes=int(found.BytesWritten),
        busy_s=at - found.IdleTime / _TICKS_PER_S,
        at_s=at,
        queue=int(found.QueueDepth),
    )


# --------------------------------------------------------------------- Linux
_SECTOR = 512


def _linux_counters(
    device: Device,
    *,
    mounts: Path = Path("/proc/mounts"),
    stats: Path = Path("/proc/diskstats"),
    clock: Callable[[], float] = time.monotonic,
) -> Counters | None:
    """The disk-statistics line for the block device mounted at ``device``."""
    name = _block_name(device, mounts)
    if name is None or not stats.is_file():
        return None
    return parse_diskstats(stats.read_text(encoding="utf-8"), name, at_s=clock())


def _block_name(mount_point: str, mounts: Path) -> str | None:
    if not mounts.is_file():
        return None
    for line in mounts.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == mount_point and parts[0].startswith("/dev/"):
            return PurePath(parts[0]).name
    return None


def parse_diskstats(text: str, name: str, *, at_s: float) -> Counters | None:
    """One device's totals out of the kernel's disk-statistics table."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 14 or parts[2] != name:
            continue
        try:
            sectors_read = int(parts[5])
            sectors_written = int(parts[9])
            in_flight = int(parts[11])
            busy_ms = int(parts[12])
        except ValueError:
            return None
        return Counters(
            read_bytes=sectors_read * _SECTOR,
            written_bytes=sectors_written * _SECTOR,
            busy_s=busy_ms / 1000.0,
            at_s=at_s,
            queue=in_flight,
        )
    return None
