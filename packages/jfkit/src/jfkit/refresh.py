"""jfkit.refresh -- one safe refresh, replacing divergent copies.

Snapshot the item, ask for a NON-replacing refresh, poll until it settles,
re-fetch, then diff everything outside the changes that were expected. A
refresh that quietly rewrites a name you fixed by hand is the failure mode
this exists to catch, and it is common enough to deserve a test.

Planned public API:
    safe_refresh(client, item_id, *, expected_changes, timeout=300) -> RefreshReport

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
