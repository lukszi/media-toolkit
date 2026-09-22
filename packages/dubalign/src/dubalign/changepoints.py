"""dubalign.changepoints -- find the seams instead of typing them in.

The measurement said where the offset jumps; finding the jumps was done by
eye. That is the single biggest gap in the original work, and it is why the
old scripts carry hand-found constants in their source.

Here the boundaries are detected from the lag series, then each one is
refined by running a local correlation at the two neighbouring offsets: the
crossing point locates the cut to a few tens of milliseconds.

A seam is then placed at the quietest instant near the change rather than at
the exact one, because a splice you cannot hear is the actual goal.

Planned public API:
    find_changepoints(series, *, method='pelt', penalty=...) -> list[Changepoint]
    refine_boundary(a, b, lag_before, lag_after, t0, t1) -> float

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
