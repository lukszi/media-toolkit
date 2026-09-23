"""jfkit.devices -- which physical device backs this path.

The first version of the gate compared the strings a media program prints,
which include a scheme prefix on some platforms and not on others, so half
the paths landed in the wrong lane and two heavy readers ran on one device.

Resolve the path to its mount point or volume root first and compare THAT.
The lesson generalises: never key a device decision on a string somebody else
formatted.

Planned public API:
    device_of(path: Path) -> DeviceId
    is_rotational(device: DeviceId) -> bool | None

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
