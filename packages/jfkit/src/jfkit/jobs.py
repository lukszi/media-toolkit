"""jfkit.jobs -- detached jobs and one heavy reader per device.

A folder of near-identical wrapper scripts collapses into one template that
emits either a scheduled task or a transient user service, depending on the
platform, and nothing shell-specific survives into the library.

The device gate is the useful part. Mechanical storage serves one sequential
reader well and two badly, so work is grouped by the device that backs each
path and only one heavy reader per device runs at a time. The gate also waits
for the media server's own background transcoding to go quiet, because it is
a reader too and it does not announce itself.

Lanes are packed largest-first so they finish together instead of leaving one
lane running alone for an hour.

Planned public API:
    launch_detached(command, *, name, log) -> JobHandle
    device_of(path: Path) -> DeviceId
    pack_lanes(items, *, key=size) -> dict[DeviceId, list[Item]]

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
