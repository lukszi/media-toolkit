"""dubalign.loudness -- integrated loudness, range and true peak.

Filling a gap means matching integrated loudness, not peak level. Matching
peaks leaves an audible step exactly where the listener is already listening
for one.

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
