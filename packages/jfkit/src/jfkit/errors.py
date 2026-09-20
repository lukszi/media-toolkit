"""jfkit.errors -- the error types, so callers can act on them.

A missing optional dependency raises an error naming the exact install line.
A missing external program raises one naming every location that was tried.

Planned public API:
    ToolNotFound
    ExtraRequired
    ServerRefused
    ItemNotFound

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
