"""jfkit.segments -- scope a segment scan to media that is not covered yet.

Plugin and task identifiers are looked up through the server's own plugin and
scheduled-task endpoints. A hardcoded plugin instance id is not a cosmetic
problem: it makes the call succeed against the wrong target, or silently do
nothing, which is worse than an error.

Planned public API:
    uncovered_items(client, *, plugin) -> list[ItemRef]
    queue_scan(client, items, *, dry_run=True) -> ScanReport

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
