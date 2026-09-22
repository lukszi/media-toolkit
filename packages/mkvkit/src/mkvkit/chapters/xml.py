"""mkvkit.chapters.xml -- build chapter XML, and check it before writing it.

The self-check is not optional: marks strictly increasing, the first one
early, the last one comfortably before the end. A set that fails any of these
describes a different cut of the film and must not be written.

A rollback document is produced from the existing chapters first, every time,
including when there are none -- 'there were none' is exactly the state that
is hardest to restore from memory.

Note the muxer adds an edition when chapters are passed, so a file can end up
with them twice unless the existing set is explicitly suppressed.

Planned public API:
    build_xml(marks, names=None) -> str
    selfcheck(marks, *, runtime_s) -> list[Problem]
    rollback_xml(path) -> str

Status: skeleton. The behaviour lands in a later milestone; this file exists
so the history shows when the work it comes from was actually done.
"""

from __future__ import annotations

# SPDX-License-Identifier: LicenseRef-PolyForm-Noncommercial-1.0.0

__all__: list[str] = []


def _not_yet(name: str) -> None:
    raise NotImplementedError(f"{name}: skeleton only, see the module docstring")
