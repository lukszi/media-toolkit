"""dubalign.decode -- one sequential read per source, two outputs.

A single decode produces both the full-rate signal and the reduced analysis
copy. Reading a large file twice off mechanical storage costs more than the
entire measurement that follows, so it is read once.

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
