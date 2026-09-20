"""jfkit.service -- service control behind a protocol, never an assumption.

Three implementations. The manual one is the DEFAULT: it prints what to do
and waits for confirmation. Nothing in the library ever assumes it is allowed
to stop somebody's server.

Planned public API:
    class ServiceController(Protocol): stop() / start() / is_running()
    WindowsServiceController / SystemdServiceController / ManualServiceController

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
