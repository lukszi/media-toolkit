"""jfkit.swap -- replace a file in place without losing its identity.

The ordering is the whole design:

  wait until nobody is watching -> stop the service -> MOVE the original to
  the parked directory (never delete it) -> copy the replacement into the
  original's exact path -> RE-PROBE the result (never a size check: a
  legitimately swapped file legitimately changes size) -> restore the
  original on any failure -> start the service -> non-replacing refresh.

Work is chunked by target size rather than by file count, so the window in
which the server is down is bounded regardless of how big the files are.

Swapping in place under the same path is what preserves item ids, play state
and every reference other tooling holds.

Planned public API:
    swap(pairs, *, controller, parked, chunk_gib=200.0, dry_run=True) -> SwapReport

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
