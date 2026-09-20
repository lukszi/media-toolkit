"""dubalign.epk_align -- excerpt alignment that survives a leading gap.

Stream-copy an excerpt keeping timestamps, read each stream's true first
packet timestamp, and correct the index-space lag by the difference:

    true_lag = index_lag + (first_pts[b] - first_pts[a])

Nothing is assumed about how the decoder handles a gap at the head. This is
the single correction that turns a confidently wrong number into a right one.

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
