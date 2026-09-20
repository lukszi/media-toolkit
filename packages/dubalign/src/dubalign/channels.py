"""dubalign.channels -- prove the two sources agree on channel order.

A correlation matrix between the channels of the two sources. A clean diagonal
means the interleave matches; anything else means a mix would quietly put the
centre channel somewhere else.

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
