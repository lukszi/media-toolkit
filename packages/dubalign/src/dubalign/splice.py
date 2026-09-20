"""dubalign.splice -- assemble the segments with equal-power crossfades.

Sine/cosine crossfades over a few milliseconds. A linear fade dips in the
middle, and a long fade is audible as a swell even when the alignment is
perfect.

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
