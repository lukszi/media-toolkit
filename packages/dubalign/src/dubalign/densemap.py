"""dubalign.densemap -- a coarse offset map across the whole timeline.

An envelope search in overlapping windows, edge to edge. The output is a lag
series, not a single number: the point is to see where the offset is constant
and where it steps.

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
